"""LAN scan: find devices in a directly connected IPv4 network.

How it works - no root privileges and no extra tools are needed:

1. Discovery (ARP). For every address in the range one empty UDP datagram is
   sent. Before the kernel can deliver it, it must learn the target's MAC
   address via ARP - and every IPv4 device on the segment has to answer ARP,
   even one whose firewall drops ping. The answers end up in the kernel
   neighbour table, which is read with `ip -j neigh`.
2. Vendor. The first 24/28/36 bits of the MAC (OUI) are looked up in the IEEE
   registry (Debian package `ieee-data`). Locally administered MACs (phones
   with "private Wi-Fi address", VMs) have no vendor.
3. Name. Best effort, in this order: DHCP lease of our own access point, mDNS
   (Apple, Linux/avahi, printers), NetBIOS (Windows, NAS) and reverse DNS.
   Many devices announce no name at all - an empty name is normal.

Only directly connected networks are scanned: behind a router there is no ARP,
so MAC and vendor would not exist.
"""

from __future__ import annotations

import csv
import glob
import ipaddress
import json
import logging
import random
import re
import select
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models import ScanHost
from .runner import CommandError, run

log = logging.getLogger("netpanel.scan")

MAX_ADDRESSES = 1024         # biggest range: /22
BATCH = 256                  # addresses probed at once (kernel neighbour table limits)
SOCKET_CHUNK = 32            # datagrams per socket (keeps us under the socket send buffer)
DISCARD_PORT = 9             # the probe payload is irrelevant, only the ARP lookup matters
POLL_S = 0.4
MIN_WAIT_S = 1.2             # give fast responders a moment even if nothing is pending
MAX_WAIT_S = 10.0            # STALE entries need DELAY (5 s) + PROBE (3 x 1 s) to be re-verified
NAME_TIMEOUT_S = 2.5

ALIVE = {"REACHABLE", "PERMANENT", "NOARP"}
PENDING = {"INCOMPLETE", "DELAY", "PROBE"}

OUI_CSV = ("/usr/share/ieee-data/oui.csv", "/usr/share/ieee-data/mam.csv", "/usr/share/ieee-data/oui36.csv")
OUI_FALLBACKS = (
    ("/usr/share/arp-scan/ieee-oui.txt", "tab"),
    ("/usr/share/nmap/nmap-mac-prefixes", "space"),
    ("/usr/share/wireshark/manuf", "manuf"),
)
LEASE_GLOB = "/var/lib/NetworkManager/dnsmasq-*.leases"
SKIP_IFACES = ("lo", "docker", "veth", "virbr", "tailscale", "wg", "tun", "tap")


# --------------------------------------------------------------------------- data


@dataclass
class LocalNet:
    iface: str
    address: str                     # our own address on that interface
    network: ipaddress.IPv4Network
    mac: str = ""

    def to_json(self) -> dict:
        return {"iface": self.iface, "address": self.address, "network": str(self.network.network_address),
                "prefix": self.network.prefixlen, "cidr": str(self.network)}


@dataclass
class Neigh:
    ip: str
    mac: str
    state: Set[str]


class ScanBusy(Exception):
    pass


# --------------------------------------------------------------------------- parsers (pure, unit tested)


NO_ARP_FLAGS = {"LOOPBACK", "POINTOPOINT", "NOARP"}


def link_skip_reason(link: dict) -> str:
    """Why an interface cannot be ARP-scanned ('' = usable).

    Note: with a family filter (`ip -4 ...`) iproute2 omits link_type and the MAC
    address, so a missing link_type is accepted and the flags decide.
    """
    name = link.get("ifname", "")
    flags = set(link.get("flags") or [])
    if not name:
        return "no name"
    if flags & NO_ARP_FLAGS:
        return "no ARP (%s)" % ",".join(sorted(flags & NO_ARP_FLAGS))
    if name.startswith(SKIP_IFACES):
        return "virtual/VPN interface"
    if link.get("link_type") not in (None, "ether"):
        return "link type %s" % link.get("link_type")
    if "UP" not in flags:
        return "interface is down"
    if not any(a.get("family") == "inet" and a.get("scope", "global") == "global" and int(a.get("prefixlen", 32)) < 32
               for a in link.get("addr_info") or []):
        return "no IPv4 network address"
    return ""


def parse_addrs(text: str) -> List[LocalNet]:
    """`ip -j addr show` -> directly connected Ethernet-like IPv4 networks."""
    out: List[LocalNet] = []
    for link in json.loads(text or "[]"):
        if link_skip_reason(link):
            continue
        name = link["ifname"]
        for a in link.get("addr_info") or []:
            if a.get("family") != "inet" or a.get("scope", "global") != "global":
                continue
            plen = int(a.get("prefixlen", 32))
            if plen >= 32:
                continue
            net = ipaddress.IPv4Network("%s/%d" % (a["local"], plen), strict=False)
            out.append(LocalNet(name, a["local"], net, (link.get("address") or "").lower()))
    return out


def parse_neigh(text: str) -> Dict[str, Neigh]:
    """`ip -j -4 neigh show dev X` -> {ip: Neigh}."""
    out: Dict[str, Neigh] = {}
    for e in json.loads(text or "[]"):
        ip = e.get("dst", "")
        if not ip or ":" in ip:
            continue
        out[ip] = Neigh(ip, (e.get("lladdr") or "").lower(), set(e.get("state") or []))
    return out


def parse_default_gateways(text: str) -> Set[str]:
    return {r["gateway"] for r in json.loads(text or "[]") if r.get("gateway")}


def scan_targets(net: ipaddress.IPv4Network, local: LocalNet) -> List[str]:
    """Every address of the range except the interface's network/broadcast address."""
    skip = set()
    if local.network.prefixlen <= 30:
        skip = {local.network.network_address, local.network.broadcast_address}
    return [str(ip) for ip in net if ip not in skip]


def is_random_mac(mac: str) -> bool:
    """Locally administered bit set: randomised ("private") or virtual MAC."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


_IP_JUNK = "(?<![0-9]){}[^0-9]+{}[^0-9]+{}[^0-9]+{}(?![0-9])"


def plausible_dns_name(name: str, ip: str) -> bool:
    """Reject generic PTR names that only spell the address (192-168-1-5.isp.net)."""
    if not name or name == ip:
        return False
    o = ip.split(".")
    for parts in (o, list(reversed(o))):
        if re.search(_IP_JUNK.format(*parts), name):
            return False
    return True


def clean_name(name: str) -> str:
    name = (name or "").strip().rstrip(".")
    if name.lower().endswith(".local"):
        name = name[:-6]
    return name


# ----- DNS / mDNS wire format


def dns_query(qid: int, name: str, qtype: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p for p in name.encode("ascii").split(b".") if p)
    return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", qtype, 1)


def reverse_name(ip: str) -> str:
    return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"


def _read_name(buf: bytes, off: int) -> Tuple[str, int]:
    """Decode a (possibly compressed) DNS name. Returns (name, offset after it)."""
    labels: List[str] = []
    end = None
    jumps = 0
    while True:
        if off >= len(buf):
            raise ValueError("truncated name")
        n = buf[off]
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                raise ValueError("truncated pointer")
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | buf[off + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("pointer loop")
            continue
        if n == 0:
            off += 1
            break
        labels.append(buf[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if end is not None else off)


def parse_ptr_response(buf: bytes) -> Optional[str]:
    """First PTR answer of a DNS/mDNS response, or None."""
    try:
        _qid, flags, qd, an = struct.unpack("!HHHH", buf[:8])
        if not flags & 0x8000:
            return None  # not a response
        off = 12
        for _ in range(qd):
            _n, off = _read_name(buf, off)
            off += 4
        for _ in range(an):
            _n, off = _read_name(buf, off)
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", buf[off:off + 10])
            off += 10
            if rtype == 12:
                target, _ = _read_name(buf, off)
                return target or None
            off += rdlen
    except (ValueError, struct.error, IndexError):
        return None
    return None


# ----- NetBIOS node status (UDP 137)

NBSTAT_NAME = b"\x20" + b"CK" + b"AA" * 15 + b"\x00"   # "*" padded, first-level encoded


def nbstat_query(qid: int) -> bytes:
    return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + NBSTAT_NAME + struct.pack("!HH", 0x21, 1)


def parse_nbstat_response(buf: bytes) -> Optional[str]:
    """Workstation name (suffix 0x00, unique) from a node status response."""
    try:
        qd, an = struct.unpack("!HH", buf[4:8])
        if an < 1:
            return None
        off = 12
        for _ in range(qd):
            _n, off = _read_name(buf, off)
            off += 4
        _n, off = _read_name(buf, off)
        rtype, _cls, _ttl, _rdlen = struct.unpack("!HHIH", buf[off:off + 10])
        off += 10
        if rtype != 0x21:
            return None
        count = buf[off]
        off += 1
        names = []
        for _ in range(count):
            raw = buf[off:off + 15]
            suffix = buf[off + 15]
            flags = struct.unpack("!H", buf[off + 16:off + 18])[0]
            off += 18
            names.append((raw.decode("ascii", "replace").rstrip(" \x00"), suffix, bool(flags & 0x8000)))
    except (ValueError, struct.error, IndexError):
        return None
    for want in (0x00, 0x20):
        for name, suffix, group in names:
            if suffix == want and not group and name:
                return name
    return None


def parse_leases(text: str) -> Dict[str, str]:
    """dnsmasq lease file -> {ip: hostname}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] != "*":
            out[parts[2]] = parts[3]
    return out


# --------------------------------------------------------------------------- vendor database


_SUFFIX = re.compile(
    r"[\s,.]*(?:co\.?,?\s*ltd|corporation|corp|incorporated|inc|ltd|limited|llc|l\.l\.c|gmbh(?:\s*&\s*co\.?\s*kg)?"
    r"|co\.?\s*kg|ag|s\.a|sa|s\.r\.l|srl|b\.v|bv|oy|ab|a/s|plc|pte|pty|sas|s\.p\.a|spa|technologies|technology"
    r"|electronics|co)\.?\s*$", re.I)


def short_vendor(name: str) -> str:
    out = (name or "").strip()
    for _ in range(4):
        new = _SUFFIX.sub("", out).strip(" ,.")
        if new == out or not new:
            break
        out = new
    return out or (name or "").strip()


class OuiDB:
    """MAC prefix -> organisation (IEEE MA-L / MA-M / MA-S). Loaded lazily, once."""

    def __init__(self, csv_paths: Sequence[str] = OUI_CSV, fallbacks=OUI_FALLBACKS):
        self.csv_paths = csv_paths
        self.fallbacks = fallbacks
        self._map: Optional[Dict[str, str]] = None
        self._lock = threading.Lock()
        self.source = ""

    def _load(self) -> Dict[str, str]:
        m: Dict[str, str] = {}
        used = []
        for path in self.csv_paths:
            try:
                with open(path, newline="", encoding="utf-8", errors="replace") as f:
                    for row in csv.reader(f):
                        if len(row) >= 3 and re.fullmatch(r"[0-9A-Fa-f]{6,9}", row[1] or ""):
                            m[row[1].upper()] = row[2].strip()
                used.append(path)
            except OSError:
                continue
        if not m:
            for path, fmt in self.fallbacks:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            self._fallback_line(m, line, fmt)
                    used.append(path)
                    break
                except OSError:
                    continue
        self.source = ", ".join(used)
        if m:
            log.info("vendor database: %d prefixes from %s", len(m), self.source)
        else:
            log.warning("no MAC vendor database found - install the 'ieee-data' package")
        return m

    @staticmethod
    def _fallback_line(m: Dict[str, str], line: str, fmt: str) -> None:
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            return
        if fmt == "manuf":
            parts = line.split("\t")
            if len(parts) < 2:
                return
            key, _, bits = parts[0].partition("/")
            hexs = key.replace(":", "").replace("-", "").upper()
            nibbles = int(bits) // 4 if bits else 6
            name = parts[2] if len(parts) > 2 and parts[2] else parts[1]
            m[hexs[:nibbles]] = name.strip()
            return
        key, _, name = line.partition("\t" if fmt == "tab" else " ")
        key = key.strip().upper()
        if re.fullmatch(r"[0-9A-F]{6,9}", key):
            m[key] = name.strip()

    @property
    def available(self) -> bool:
        return bool(self.map)

    @property
    def map(self) -> Dict[str, str]:
        with self._lock:
            if self._map is None:
                self._map = self._load()
            return self._map

    def lookup(self, mac: str) -> str:
        hexs = mac.replace(":", "").replace("-", "").upper()
        m = self.map
        for n in (9, 7, 6):
            name = m.get(hexs[:n])
            if name:
                return name
        return ""


# --------------------------------------------------------------------------- the real scanner


def _chunks(seq: Sequence[str], n: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class LanScanner:
    """Discovery + name resolution against the real system."""

    def __init__(self, oui: Optional[OuiDB] = None, runner: Callable = run, clock: Callable = time.monotonic):
        self.oui = oui or OuiDB()
        self._run = runner
        self._clock = clock

    # ---- system information
    def local_networks(self) -> List[LocalNet]:
        return parse_addrs(self.addr_json())

    def addr_json(self) -> str:
        # No "-4" here: with a family filter iproute2 leaves out link_type and the MAC address.
        return self._run(["ip", "-j", "addr", "show"], timeout=5, quiet=True).stdout

    def gateways(self) -> Set[str]:
        try:
            return parse_default_gateways(self._run(["ip", "-j", "-4", "route", "show", "default"], timeout=5, quiet=True).stdout)
        except CommandError:
            return set()

    def neighbours(self, iface: str) -> Dict[str, Neigh]:
        return parse_neigh(self._run(["ip", "-j", "-4", "neigh", "show", "dev", iface], timeout=5, quiet=True).stdout)

    def hostname(self) -> str:
        return socket.gethostname()

    def prepare(self) -> None:
        _ = self.oui.map  # load the vendor database before the first result arrives

    def vendor(self, mac: str) -> str:
        return self.oui.lookup(mac)

    # ---- discovery
    @staticmethod
    def poke(ips: Sequence[str]) -> None:
        """Send one empty UDP datagram to each address -> the kernel ARPs for it."""
        for chunk in _chunks(ips, SOCKET_CHUNK):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            try:
                for ip in chunk:
                    try:
                        s.sendto(b"", (ip, DISCARD_PORT))
                    except OSError:
                        pass  # EAGAIN / EHOSTUNREACH: nothing to learn here
            finally:
                s.close()

    def discover(self, iface: str, targets: Sequence[str], cancel: threading.Event,
                 on_found: Callable[[str, str], None], on_progress: Callable[[int], None]) -> None:
        done_before = 0
        for batch in _chunks(list(targets), BATCH):
            if cancel.is_set():
                return
            self.poke(batch)
            start = self._clock()
            seen: Set[str] = set()
            while True:
                if cancel.wait(POLL_S):
                    return
                elapsed = self._clock() - start
                table = self.neighbours(iface)
                pending = 0
                for ip in batch:
                    n = table.get(ip)
                    if n is not None and n.state & ALIVE and n.mac:
                        if ip not in seen:
                            seen.add(ip)
                            on_found(ip, n.mac)
                    elif n is not None and (n.state & PENDING or ("STALE" in n.state and elapsed < 2.0)):
                        pending += 1
                    elif n is None and elapsed < 1.0:
                        pending += 1  # entry not created yet
                on_progress(done_before + len(batch) - pending)
                if (pending == 0 and elapsed >= MIN_WAIT_S) or elapsed >= MAX_WAIT_S:
                    break
            done_before += len(batch)
            on_progress(done_before)

    # ---- names
    def resolve_names(self, hosts: Sequence[Tuple[str, str]], cancel: threading.Event,
                      timeout: float = NAME_TIMEOUT_S) -> Dict[str, Dict[str, str]]:
        """hosts = [(ip, mac)] -> {ip: {"dhcp"|"mdns"|"netbios"|"dns": name}}."""
        ips = [ip for ip, _ in hosts]
        names: Dict[str, Dict[str, str]] = {ip: {} for ip in ips}
        if not ips:
            return names

        for path in glob.glob(LEASE_GLOB):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for ip, name in parse_leases(f.read()).items():
                        if ip in names:
                            names[ip]["dhcp"] = name
            except OSError:
                pass

        pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="scan-rdns")
        futures = {pool.submit(self._rdns, ip): ip for ip in ips}
        udp = {}
        t_m = threading.Thread(target=lambda: udp.update(mdns=self._udp_sweep(ips, 5353, self._mdns_packet, parse_ptr_response, cancel, timeout - 0.5)))
        t_n = threading.Thread(target=lambda: udp.update(netbios=self._udp_sweep(ips, 137, lambda ip, qid: nbstat_query(qid), parse_nbstat_response, cancel, timeout - 0.5)))
        for t in (t_m, t_n):
            t.daemon = True
            t.start()
        end = time.monotonic() + timeout
        done: Set = set()
        while not cancel.is_set():
            left = end - time.monotonic()
            done, pending = wait(futures, timeout=min(0.25, max(0.0, left)))
            if not pending or left <= 0:
                break
        pool.shutdown(wait=False, cancel_futures=True)
        for t in (t_m, t_n):
            t.join(timeout=1.0)

        for fut in done:
            ip = futures[fut]
            try:
                name = fut.result()
            except Exception:  # pragma: no cover - resolver errors are expected
                name = ""
            if name and plausible_dns_name(name, ip):
                names[ip]["dns"] = name.rstrip(".")
        for source in ("mdns", "netbios"):
            for ip, name in (udp.get(source) or {}).items():
                name = clean_name(name)
                if ip in names and name:
                    names[ip][source] = name
        return names

    @staticmethod
    def _rdns(ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            return ""

    @staticmethod
    def _mdns_packet(ip: str, qid: int) -> bytes:
        return dns_query(qid, reverse_name(ip), 12)

    @staticmethod
    def _udp_sweep(ips: Sequence[str], port: int, make: Callable[[str, int], bytes],
                   parse: Callable[[bytes], Optional[str]], cancel: threading.Event, timeout: float) -> Dict[str, str]:
        """Send one query to every host from one socket, collect the answers by source address."""
        out: Dict[str, str] = {}
        wanted = set(ips)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return out
        try:
            s.setblocking(False)
            for ip in ips:
                try:
                    s.sendto(make(ip, random.randint(1, 0xFFFF)), (ip, port))
                except OSError:
                    pass
            end = time.monotonic() + max(0.3, timeout)
            while not cancel.is_set() and len(out) < len(wanted):
                left = end - time.monotonic()
                if left <= 0:
                    break
                r, _, _ = select.select([s], [], [], min(left, 0.3))
                if not r:
                    continue
                try:
                    data, addr = s.recvfrom(4096)
                except OSError:
                    continue  # ICMP port unreachable surfaces here as ECONNREFUSED
                if addr[0] in wanted and addr[0] not in out:
                    name = parse(data)
                    if name:
                        out[addr[0]] = name
        finally:
            s.close()
        return out


# --------------------------------------------------------------------------- job manager


NAME_ORDER = ("dhcp", "mdns", "netbios", "dns")


def pick_name(names: Dict[str, str]) -> Tuple[str, str]:
    for src in NAME_ORDER:
        if names.get(src):
            return names[src], src
    return "", ""


class ScanManager:
    """Runs one scan at a time in a background thread; the API polls snapshot()."""

    def __init__(self, engine, clock: Callable = time.time):
        self.engine = engine
        self._clock = clock
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._job = self._empty()
        self._hosts: Dict[str, ScanHost] = {}

    @staticmethod
    def _empty() -> dict:
        return {"id": 0, "state": "idle", "phase": "", "range": "", "iface": "", "total": 0, "probed": 0,
                "started": None, "finished": None, "duration": None, "message": "", "vendor_db": True}

    # ---- public
    def local_networks(self) -> List[LocalNet]:
        return self.engine.local_networks()

    @property
    def running(self) -> bool:
        return self._job["state"] == "running"

    def start(self, net: ipaddress.IPv4Network, local: LocalNet) -> dict:
        with self._lock:
            if self.running:
                raise ScanBusy()
            self._seq += 1
            self._cancel = threading.Event()
            targets = scan_targets(net, local)
            self._job = dict(self._empty(), id=self._seq, state="running", phase="discover", range=str(net),
                             iface=local.iface, total=len(targets), started=self._clock())
            self._hosts = {}
            self._thread = threading.Thread(target=self._run, args=(self._seq, net, local, targets, self._cancel),
                                            name="lan-scan", daemon=True)
            self._thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            job = dict(self._job)
            hosts = sorted(self._hosts.values(), key=lambda h: ipaddress.IPv4Address(h.ip))
            job["hosts"] = [h.to_json() for h in hosts]
            job["count"] = len(hosts)
        return job

    def wait(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- worker
    def _update(self, seq: int, **kw) -> None:
        with self._lock:
            if self._job["id"] == seq:
                self._job.update(kw)

    def _add(self, seq: int, ip: str, mac: str, tags=()) -> None:
        random_mac = is_random_mac(mac)
        vendor = "" if random_mac else self.engine.vendor(mac)
        host = ScanHost(ip=ip, mac=mac, vendor=short_vendor(vendor), vendor_full=vendor,
                        random_mac=random_mac, tags=list(tags))
        if "self" in tags:
            host.name, host.name_source = self.engine.hostname(), "this device"
        with self._lock:
            if self._job["id"] == seq and ip not in self._hosts:
                self._hosts[ip] = host

    def _run(self, seq: int, net, local: LocalNet, targets: List[str], cancel: threading.Event) -> None:
        log.info("LAN scan %s on %s (%d addresses)", net, local.iface, len(targets))
        state, message = "done", ""
        try:
            self.engine.prepare()
            self._update(seq, vendor_db=bool(getattr(getattr(self.engine, "oui", None), "available", True)))
            # this device never shows up in its own neighbour table
            if ipaddress.IPv4Address(local.address) in net:
                self._add(seq, local.address, local.mac, ["self"])
            self.engine.discover(local.iface, targets, cancel,
                                 on_found=lambda ip, mac: self._add(seq, ip, mac),
                                 on_progress=lambda n: self._update(seq, probed=min(n, len(targets))))
            gws = self.engine.gateways()
            with self._lock:
                for ip, h in self._hosts.items():
                    if ip in gws and "gateway" not in h.tags:
                        h.tags.append("gateway")
                todo = [(ip, h.mac) for ip, h in self._hosts.items() if "self" not in h.tags]
            if not cancel.is_set():
                self._update(seq, phase="names", probed=len(targets))
                names = self.engine.resolve_names(todo, cancel)
                with self._lock:
                    for ip, found in names.items():
                        h = self._hosts.get(ip)
                        if h is not None and found:
                            h.names = dict(found)
                            h.name, h.name_source = pick_name(found)
            if cancel.is_set():
                state = "stopped"
        except CommandError as e:
            state, message = "error", e.message
        except Exception as e:  # pragma: no cover - defensive
            log.exception("LAN scan failed")
            state, message = "error", str(e)
        finished = self._clock()
        with self._lock:
            if self._job["id"] == seq:
                self._job.update(state=state, phase="", message=message, finished=finished,
                                 duration=round(finished - (self._job["started"] or finished), 1))
            count = len(self._hosts)
        log.info("LAN scan %s: %s, %d devices", net, state, count)


def suggest_range(eth_net: Optional[ipaddress.IPv4Network], eth_iface: str, nets: List[LocalNet]) -> Optional[dict]:
    """Default range: the Ethernet network (or the first local one), at most /22."""
    local = None
    if eth_net is not None:
        local = next((n for n in nets if n.iface == eth_iface and n.network == eth_net), None)
    if local is None and nets:
        local = next((n for n in nets if n.iface == eth_iface), nets[0])
    if local is None:
        return None
    net, note = local.network, ""
    if net.num_addresses > MAX_ADDRESSES:
        net = ipaddress.IPv4Network("%s/24" % local.address, strict=False)
        note = "%s is a /%d - suggesting the /24 around this device." % (local.iface, local.network.prefixlen)
    return {"network": str(net.network_address), "prefix": net.prefixlen, "source": local.iface, "note": note}


# --------------------------------------------------------------------------- CLI (debugging over SSH)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one scan from the shell and print the table - same code path as the panel:

        cd /opt/netpanel && sudo -u netpanel python3 -m netpanel.backend.lanscan [192.168.1.0/24]
    """
    import argparse

    from ..validation import ValidationError, validate_scan_range

    ap = argparse.ArgumentParser(prog="python3 -m netpanel.backend.lanscan", description=main.__doc__)
    ap.add_argument("range", nargs="?", help="network/prefix, default: the eth0 network")
    ap.add_argument("--eth", default="eth0")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    scanner = LanScanner()
    try:
        nets = scanner.local_networks()
    except CommandError as e:
        print("error:", e.message)
        return 2
    print("directly connected:", ", ".join("%s %s" % (n.iface, n.network) for n in nets) or "none")
    if not nets:
        print("interfaces seen (`ip -j addr show`):")
        for link in json.loads(scanner.addr_json() or "[]"):
            inet = [("%s/%s" % (a.get("local"), a.get("prefixlen"))) for a in link.get("addr_info") or []
                    if a.get("family") == "inet"]
            print("  %-12s %-28s %s" % (link.get("ifname"), ",".join(inet) or "-",
                                         link_skip_reason(link) or "usable (?)"))
    if args.range:
        network, _, prefix = args.range.partition("/")
        prefix = prefix or "24"
    else:
        s = suggest_range(None, args.eth, nets)
        if not s:
            print("no IPv4 network to scan")
            return 1
        network, prefix = s["network"], s["prefix"]
    try:
        net, local = validate_scan_range(network, prefix, nets, MAX_ADDRESSES)
    except ValidationError as e:
        print("error:", e.message)
        return 2
    mgr = ScanManager(scanner)
    mgr.start(net, local)
    print("scanning %s on %s ..." % (net, local.iface))
    try:
        while mgr.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        mgr.stop()
    snap = mgr.snapshot()
    print("vendor database:", scanner.oui.source or "MISSING (sudo apt install ieee-data)")
    print("%-28s %-16s %-18s %s" % ("NAME", "IP", "MAC", "VENDOR"))
    for h in snap["hosts"]:
        vendor = "(private MAC)" if h["random_mac"] else h["vendor"] or "?"
        name = (h["name"] or "-") + (" [%s]" % ",".join(h["tags"]) if h["tags"] else "")
        print("%-28s %-16s %-18s %s" % (name[:28], h["ip"], h["mac"], vendor))
    print("%s: %d devices, %s addresses in %s s" % (snap["state"], snap["count"], snap["total"], snap["duration"]))
    return 0 if snap["state"] in ("done", "stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
