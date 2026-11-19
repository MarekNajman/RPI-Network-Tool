"""Camera finder: Milesight cameras / NVRs and any ONVIF camera on the local network.

Two discovery protocols run side by side - no root privileges and no extra
packages are needed (plain UDP sockets):

1. Milesight "udpp" - the protocol of Milesight Smart Tools. Recovered from
   Smart Tools 2.4.0.5 for interoperability (static analysis, not a published
   spec - see README):
     * UDP multicast 239.255.255.250, port 26360 (source port 26360 as well);
     * payload = XML document + 2-byte checksum (16-bit one's complement sum of
       the XML bytes, little-endian words, like the IP header checksum);
     * <Query><Id>..</Id><Types>search</Types></Query> asks every Milesight
       camera to answer with <Reply> (IP, mask, gateway, model, firmware, MAC…).
       Replies are multicast too, so cameras in a *different* subnet are found
       as well (e.g. a factory-reset camera on 192.168.5.190);
     * <Types>modify</Types> addressed by MAC changes the camera's IP settings
       (user name + MD5 of the password) - works across subnets.
2. ONVIF WS-Discovery (port 3702) - finds ONVIF cameras of any brand in the
   same subnet (they answer by unicast).

Not implemented: the raw-Ethernet variant of the Milesight protocol (needs
CAP_NET_RAW), camera activation (RSA challenge), firmware upgrade.

Debug from SSH (same code as the panel):
    python3 -m netpanel.backend.cameras                 # search on eth0
    python3 -m netpanel.backend.cameras wlan0 --debug   # print every datagram
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import select
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

from ..models import Camera
from .lanscan import LocalNet, ScanBusy, is_random_mac, short_vendor
from .runner import CommandError

log = logging.getLogger("netpanel.cams")

MS_GROUP = "239.255.255.250"
MS_PORT = 26360
WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702
XML_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\r\n'

DISCOVER_S = 4.0                    # one search round
MS_SEND_AT = (0.0, 0.8, 2.0)        # UDP may get lost - ask three times
WSD_SEND_AT = (0.1, 1.5)
MODIFY_WAIT_S = 4.0
MODIFY_TRIES = 2
MAX_XML = 16 * 1024                 # Milesight replies are ~1 kB
MAX_WSD = 64 * 1024

WSD_TYPES = ("dn:NetworkVideoTransmitter", "tds:Device")


class CameraError(Exception):
    """Operation failed - `message` is short and suitable for the UI."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------- Milesight protocol (pure, unit tested)


def ms_checksum(data: bytes) -> int:
    """16-bit one's complement sum over little-endian words (Smart Tools FUN_007a0430)."""
    total = 0
    n = len(data)
    for i in range(0, n - 1, 2):
        total += data[i] | (data[i + 1] << 8)
    if n % 2:
        total += data[-1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def ms_frame(xml: str) -> bytes:
    body = xml.encode("utf-8")
    return body + struct.pack("<H", ms_checksum(body))


def ms_unframe(data: bytes) -> Tuple[bytes, Optional[bool]]:
    """-> (xml bytes, checksum_ok). checksum_ok is None when the datagram has no trailer."""
    if len(data) > 2:
        body, tail = data[:-2], data[-2:]
        if struct.unpack("<H", tail)[0] == ms_checksum(body):
            return body, True
    stripped = data.rstrip(b"\x00\r\n\t ")
    if stripped.endswith(b">"):
        return stripped, None
    # bad checksum: drop the 2 trailing bytes and let the XML parser decide
    return data[:-2] if len(data) > 2 else data, False


def ms_xml(root: str, items: Iterable[Tuple[str, object]]) -> str:
    """Flat XML document in the layout CMarkup (used by Smart Tools) produces."""
    out = [XML_HEAD, "<%s>\r\n" % root]
    for tag, value in items:
        out.append("<%s>%s</%s>\r\n" % (tag, escape("" if value is None else str(value)), tag))
    out.append("</%s>\r\n" % root)
    return "".join(out)


def mac_bytes(mac: str) -> bytes:
    hexes = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(hexes) != 12:
        raise ValueError("invalid MAC address: %r" % mac)
    return bytes.fromhex(hexes)


def mac_colon(mac: str) -> str:
    return ":".join("%02x" % b for b in mac_bytes(mac))


def ms_id(local_mac: str, guid: Optional[uuid.UUID] = None) -> str:
    """Query Id: our MAC (12 hex) + a GUID, exactly as Smart Tools formats it.

    Cameras echo the Id; Smart Tools only checks the MAC part."""
    try:
        m = mac_bytes(local_mac)
    except ValueError:
        m = b"\x00" * 6
    g = guid or uuid.uuid4()
    d4 = g.bytes[8:]
    return ("%02x%02x%02x%02x%02x%02x-%08X-%04X-%04x-%02X%02X-%02X%02X%02X%02X%02X%02X"
            % (tuple(m) + (g.time_low, g.time_mid, g.time_hi_version) + tuple(d4)))


def ms_search_query(local_mac: str, nvr: bool = False, guid: Optional[uuid.UUID] = None) -> bytes:
    items: List[Tuple[str, object]] = [("Id", ms_id(local_mac, guid)), ("Types", "search")]
    if nvr:
        items.append(("DeviceTypes", "NVR"))
    return ms_frame(ms_xml("Query", items))


def password_digest(password: str) -> str:
    """Smart Tools sends MD5(password) as lowercase hex (QCryptographicHash::Md5)."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def ms_modify_query(local_mac: str, cam_mac: str, address: str, netmask: str, gateway: str, dns: str,
                    http_port: int, device_name: str, username: str, password: str,
                    guid: Optional[uuid.UUID] = None) -> bytes:
    return ms_frame(ms_xml("Query", [
        ("Id", ms_id(local_mac, guid)),
        ("Types", "modify"),
        ("MAC", mac_colon(cam_mac)),
        ("IPv4Address", address),
        ("HttpPort", int(http_port)),
        ("IPv4SubnetMask", netmask),
        ("IPv4Gateway", gateway),
        ("DNSIp", dns),
        ("DeviceName", device_name),
        ("UserName", username),
        ("Password", password_digest(password)),
    ]))


_FLAT_RE = re.compile(rb"<([A-Za-z_][\w.-]*)>([^<]*)</\1>")
_ROOT_RE = re.compile(rb"<([A-Za-z_][\w.-]*)>")


def _unescape(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def parse_ms(data: bytes) -> Optional[dict]:
    """Milesight datagram -> {"root": "Reply", "fields": {...}, "checksum": bool|None} or None."""
    if not data or len(data) > MAX_XML or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    body, ok = ms_unframe(data)
    fields: Dict[str, str] = {}
    root = ""
    try:
        start = body.find(b"<", body.find(b"?>") + 2 if b"?>" in body else 0)
        tree = ET.fromstring(body[start:].decode("utf-8", "replace"))
        root = tree.tag
        for child in tree:
            if len(child) == 0:
                fields[child.tag] = (child.text or "").strip()
    except (ET.ParseError, ValueError):
        # Fallback for sloppy firmware (e.g. unescaped '&'): flat <Tag>value</Tag> pairs.
        head = body.split(b"?>", 1)[-1]
        m = _ROOT_RE.search(head)
        if not m:
            return None
        root = m.group(1).decode("ascii", "replace")
        for tag, value in _FLAT_RE.findall(head):
            fields.setdefault(tag.decode("ascii", "replace"), _unescape(value).strip())
    if not root:
        return None
    return {"root": root, "fields": fields, "checksum": ok}


def _int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _flag(text: Optional[str]) -> Optional[bool]:
    if text is None or str(text).strip() == "":
        return None
    return _int(text, 1) != 0


def _norm_mac(mac: str) -> str:
    try:
        return mac_colon(mac)
    except ValueError:
        return ""


def camera_from_ms(fields: Dict[str, str], iface: str = "") -> Optional[Camera]:
    ip = fields.get("IPv4Address", "").strip()
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    kind = (fields.get("DeviceTypes") or "").strip().upper()
    active = fields.get("ActiveStatus")
    return Camera(
        ip=ip,
        mac=_norm_mac(fields.get("MAC", "")),
        kind="NVR" if kind == "NVR" else "IPC",
        vendor="Milesight",
        model=fields.get("Model", ""),
        oem_model=fields.get("OemModel", ""),
        name=fields.get("DeviceName", ""),
        firmware=fields.get("Software", ""),
        hardware=fields.get("Hardware", ""),
        serial=fields.get("SN", ""),
        netmask=fields.get("IPv4SubnetMask", ""),
        gateway=fields.get("IPv4Gateway", ""),
        dns=fields.get("DNSIp", ""),
        http_port=_int(fields.get("HttpPort", ""), 0),
        https_port=_int(fields.get("HttpsPort", ""), 0),
        http_enabled=_flag(fields.get("HttpEnable")),
        https_enabled=_flag(fields.get("HttpsEnable")),
        # Smart Tools: empty ActiveStatus = activated, "0" = not activated yet
        activated=True if active is None or active.strip() == "" else _int(active, 1) != 0,
        uptime=fields.get("Runtime", ""),
        sources=["milesight"],
        iface=iface,
        can_modify=True,
    )


# --------------------------------------------------------------------------- ONVIF WS-Discovery (pure, unit tested)

_NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"


def wsd_probe(types: str = "dn:NetworkVideoTransmitter", message_id: Optional[str] = None) -> bytes:
    mid = message_id or "uuid:%s" % uuid.uuid4()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        '<e:Header><w:MessageID>%s</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header><e:Body><d:Probe><d:Types>%s</d:Types></d:Probe></e:Body></e:Envelope>' % (mid, types)
    ).encode("utf-8")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_wsd(data: bytes) -> List[dict]:
    """ProbeMatches -> [{"address", "xaddrs": [...], "scopes": [...], "types"}]."""
    if not data or len(data) > MAX_WSD or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return []
    try:
        tree = ET.fromstring(data.decode("utf-8", "replace"))
    except (ET.ParseError, ValueError):
        return []
    out = []
    for pm in tree.iter():
        if _local(pm.tag) != "ProbeMatch":
            continue
        rec = {"address": "", "xaddrs": [], "scopes": [], "types": ""}
        for el in pm.iter():
            name = _local(el.tag)
            text = (el.text or "").strip()
            if name == "Address" and not rec["address"]:
                rec["address"] = text
            elif name == "XAddrs":
                rec["xaddrs"] = text.split()
            elif name == "Scopes":
                rec["scopes"] = text.split()
            elif name == "Types":
                rec["types"] = text
        out.append(rec)
    return out


def scope_values(scopes: List[str]) -> Dict[str, str]:
    """onvif://www.onvif.org/name/Front%20door -> {"name": "Front door"} (first value wins)."""
    out: Dict[str, str] = {}
    for s in scopes:
        m = re.match(r"onvif://www\.onvif\.org/([^/]+)/(.+)$", s, re.I)
        if m:
            out.setdefault(m.group(1).lower(), unquote(m.group(2)).strip())
    return out


def camera_from_wsd(rec: dict, src_ip: str, iface: str = "") -> Optional[Camera]:
    ip, port, url = src_ip, 0, ""
    for x in rec.get("xaddrs") or []:
        parts = urlsplit(x)
        try:
            host = parts.hostname or ""
            ipaddress.IPv4Address(host)
        except ValueError:
            continue
        url = x
        if host == src_ip or not ip:
            ip = host
            port = parts.port or (443 if parts.scheme == "https" else 80)
            break
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    sc = scope_values(rec.get("scopes") or [])
    types = rec.get("types", "")
    kind = "ONVIF"
    if "NetworkVideoTransmitter" not in types and "NetworkVideoDisplay" in types:
        kind = "ONVIF display"
    return Camera(
        ip=ip,
        mac=_norm_mac(sc.get("mac", "")),
        kind=kind,
        model=sc.get("hardware", ""),
        name=sc.get("name", ""),
        http_port=port,
        onvif_url=url,
        sources=["onvif"],
        iface=iface,
    )


def merge(cams: Dict[str, Camera], cam: Camera) -> Camera:
    """Add `cam` to `cams` (keyed by IP); Milesight data wins, ONVIF fills the gaps."""
    old = cams.get(cam.ip)
    if old is None:
        cams[cam.ip] = cam
        return cam
    a, b = (cam, old) if "milesight" in cam.sources and "milesight" not in old.sources else (old, cam)
    for f in a.__dataclass_fields__:
        va, vb = getattr(a, f), getattr(b, f)
        if f == "sources":
            setattr(a, f, sorted(set(va) | set(vb), key=["milesight", "onvif"].index))
        elif f == "kind" and va.startswith("ONVIF") and vb in ("IPC", "NVR"):
            a.kind = vb
        elif va in ("", 0, None, []) and vb not in ("", 0, None, []):
            setattr(a, f, vb)
    a.can_modify = a.can_modify or b.can_modify
    cams[cam.ip] = a
    return a


# --------------------------------------------------------------------------- sockets


def _ms_socket(local_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            s.bind(("", MS_PORT))
        except OSError as e:
            raise CameraError("UDP port %d is in use by another program (%s)" % (MS_PORT, e.strerror)) from None
        mreq = socket.inet_aton(MS_GROUP) + socket.inet_aton(local_ip)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log.warning("IP_ADD_MEMBERSHIP on %s failed: %s", local_ip, e)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setblocking(False)
        return s
    except Exception:
        s.close()
        raise


def _wsd_socket(local_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.bind((local_ip, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setblocking(False)
        return s
    except Exception:
        s.close()
        raise


def _send(s: socket.socket, payload: bytes, dest: Tuple[str, int], what: str) -> None:
    try:
        s.sendto(payload, dest)
    except OSError as e:
        log.warning("%s send failed: %s", what, e)


# --------------------------------------------------------------------------- engine


class CameraFinder:
    """Talks to the network. `lan` is the LanScanner (interfaces, ARP table, vendor DB)."""

    def __init__(self, lan, clock: Callable = time.monotonic, debug: Optional[Callable[[str, bytes, tuple], None]] = None):
        self.lan = lan
        self._clock = clock
        self.debug = debug

    def local_networks(self) -> List[LocalNet]:
        return self.lan.local_networks()

    def discover(self, local: LocalNet, cancel: threading.Event, on_found: Callable[[Camera], None],
                 duration: float = DISCOVER_S) -> None:
        ms = _ms_socket(local.address)
        try:
            wsd = _wsd_socket(local.address)
        except OSError as e:
            ms.close()
            raise CameraError("Cannot open a UDP socket on %s: %s" % (local.address, e.strerror)) from None
        sends = sorted([(t, "ms") for t in MS_SEND_AT] + [(t, "wsd") for t in WSD_SEND_AT])
        start = self._clock()
        try:
            while not cancel.is_set():
                now = self._clock() - start
                while sends and sends[0][0] <= now:
                    _t, what = sends.pop(0)
                    if what == "ms":
                        _send(ms, ms_search_query(local.mac), (MS_GROUP, MS_PORT), "Milesight search")
                        _send(ms, ms_search_query(local.mac, nvr=True), (MS_GROUP, MS_PORT), "Milesight NVR search")
                    else:
                        for t in WSD_TYPES:
                            _send(wsd, wsd_probe(t), (WSD_GROUP, WSD_PORT), "ONVIF probe")
                if now >= duration:
                    break
                wait = min(0.2, duration - now, (sends[0][0] - now) if sends else 0.2)
                ready, _w, _x = select.select([ms, wsd], [], [], max(0.01, wait))
                for s in ready:
                    self._drain(s, s is ms, local, on_found)
        finally:
            ms.close()
            wsd.close()

    def _drain(self, s: socket.socket, is_ms: bool, local: LocalNet, on_found) -> None:
        for _ in range(64):
            try:
                data, src = s.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                log.debug("recv: %s", e)
                return
            if self.debug:
                self.debug("milesight" if is_ms else "onvif", data, src)
            if is_ms:
                msg = parse_ms(data)
                if not msg or msg["root"] != "Reply" or (msg["fields"].get("Types", "search") != "search"):
                    continue  # our own Query (multicast loop), other Smart Tools traffic, modify replies
                cam = camera_from_ms(msg["fields"], local.iface)
                if cam:
                    on_found(cam)
            else:
                for rec in parse_wsd(data):
                    cam = camera_from_wsd(rec, src[0], local.iface)
                    if cam:
                        on_found(cam)

    def fill_macs(self, local: LocalNet, cams: List[Camera]) -> None:
        """MAC + vendor for devices found only via ONVIF (ARP table)."""
        need = [c for c in cams if not c.mac]
        if not need:
            pass
        else:
            try:
                neigh = self.lan.neighbours(local.iface)
            except CommandError:
                neigh = {}
            for c in need:
                n = neigh.get(c.ip)
                if n and n.mac:
                    c.mac = n.mac
        for c in cams:
            if not c.vendor and c.mac and not is_random_mac(c.mac):
                try:
                    c.vendor = short_vendor(self.lan.vendor(c.mac))
                except Exception:  # pragma: no cover - vendor DB is best effort
                    pass

    def modify(self, local: LocalNet, cam: Camera, cfg: dict, username: str, password: str,
               wait: float = MODIFY_WAIT_S, tries: int = MODIFY_TRIES) -> str:
        payload = ms_modify_query(local.mac, cam.mac, cfg["address"], cfg["netmask"], cfg["gateway"],
                                  cfg.get("dns") or cam.dns or cfg["gateway"], cfg["http_port"],
                                  cam.name, username, password)
        want = _norm_mac(cam.mac)
        s = _ms_socket(local.address)
        try:
            for attempt in range(tries):
                log.info("camera %s (%s): modify -> %s/%s gw %s (try %d)", cam.ip, want, cfg["address"],
                         cfg["netmask"], cfg["gateway"], attempt + 1)
                _send(s, payload, (MS_GROUP, MS_PORT), "Milesight modify")
                end = self._clock() + wait
                while self._clock() < end:
                    ready, _w, _x = select.select([s], [], [], max(0.01, end - self._clock()))
                    if not ready:
                        continue
                    try:
                        data, src = s.recvfrom(65535)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if self.debug:
                        self.debug("milesight", data, src)
                    msg = parse_ms(data)
                    if not msg or msg["root"] != "Reply" or msg["fields"].get("Types") != "modify":
                        continue
                    mac = _norm_mac(msg["fields"].get("MAC", ""))
                    if mac and want and mac != want:
                        continue
                    result = msg["fields"].get("Result", "").strip().lower()
                    if result == "failed":
                        raise CameraError("The camera refused the change - check the user name and password.")
                    return "Camera %s: new address %s requested. It may take a few seconds to apply." % (
                        cam.model or want, cfg["address"])
        finally:
            s.close()
        raise CameraError("No answer from the camera. It may be offline, or its firmware does not support this "
                          "command - use the camera's web interface instead.")


# --------------------------------------------------------------------------- job manager (used by the API)


class CameraManager:
    """One search at a time in a background thread; the API polls snapshot()."""

    def __init__(self, engine, clock: Callable = time.time, duration: float = DISCOVER_S):
        self.engine = engine
        self._clock = clock
        self.duration = duration
        self._lock = threading.Lock()
        self._op = threading.Lock()          # search and modify never overlap (same UDP port)
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._job = self._empty()
        self._cams: Dict[str, Camera] = {}
        self._local: Optional[LocalNet] = None

    @staticmethod
    def _empty() -> dict:
        return {"id": 0, "state": "idle", "iface": "", "network": "", "started": None, "finished": None,
                "duration": None, "message": ""}

    def local_networks(self) -> List[LocalNet]:
        return self.engine.local_networks()

    @property
    def running(self) -> bool:
        return self._job["state"] == "running"

    def start(self, local: LocalNet) -> dict:
        with self._lock:
            if self.running:
                raise ScanBusy()
            if not self._op.acquire(blocking=False):
                raise ScanBusy()
            self._seq += 1
            self._cancel = threading.Event()
            self._local = local
            self._job = dict(self._empty(), id=self._seq, state="running", iface=local.iface,
                             network=str(local.network), started=self._clock())
            self._cams = {}
            self._thread = threading.Thread(target=self._run, args=(self._seq, local, self._cancel),
                                            name="cam-scan", daemon=True)
            self._thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        return self.snapshot()

    def wait(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            job = dict(self._job)
            cams = sorted(self._cams.values(), key=lambda c: ipaddress.IPv4Address(c.ip))
            job["cams"] = [c.to_json() for c in cams]
            job["count"] = len(cams)
        return job

    def find(self, mac: str) -> Optional[Camera]:
        want = _norm_mac(mac)
        with self._lock:
            for c in self._cams.values():
                if want and _norm_mac(c.mac) == want:
                    return c
        return None

    def _found(self, seq: int, cam: Camera) -> None:
        net = self._local.network if self._local else None
        cam.in_subnet = bool(net and ipaddress.IPv4Address(cam.ip) in net)
        with self._lock:
            if self._job["id"] == seq:
                merge(self._cams, cam)

    def _run(self, seq: int, local: LocalNet, cancel: threading.Event) -> None:
        log.info("camera search on %s (%s)", local.iface, local.address)
        state, message = "done", ""
        try:
            self.engine.discover(local, cancel, lambda c: self._found(seq, c), self.duration)
            with self._lock:
                cams = list(self._cams.values())
            self.engine.fill_macs(local, cams)
            if cancel.is_set():
                state = "stopped"
        except (CameraError, CommandError) as e:
            state, message = "error", e.message
        except Exception as e:  # pragma: no cover - defensive
            log.exception("camera search failed")
            state, message = "error", str(e)
        finally:
            self._op.release()
        finished = self._clock()
        with self._lock:
            if self._job["id"] == seq:
                self._job.update(state=state, message=message, finished=finished,
                                 duration=round(finished - (self._job["started"] or finished), 1))
            count = len(self._cams)
        log.info("camera search on %s: %s, %d devices", local.iface, state, count)

    def modify(self, mac: str, local: LocalNet, cfg: dict, username: str, password: str) -> str:
        cam = self.find(mac)
        if cam is None or not cam.can_modify:
            raise CameraError("Camera not in the last search result - search again first.")
        if not self._op.acquire(blocking=False):
            raise CameraError("A camera search is still running - wait a moment.")
        try:
            msg = self.engine.modify(local, cam, cfg, username, password)
        finally:
            self._op.release()
        with self._lock:
            self._job["message"] = msg
        return msg


# --------------------------------------------------------------------------- CLI (SSH debugging)


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        lines.append("  %04x  %-*s %s" % (i, width * 3, " ".join("%02x" % b for b in chunk),
                                          "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)))
    return "\n".join(lines)


def _mask_secret(text: str) -> str:
    return re.sub(r"<Password>[^<]*</Password>", "<Password>***</Password>", text)


def main(argv=None) -> int:  # pragma: no cover - interactive helper
    import argparse
    import getpass
    import json as _json
    import sys

    from .lanscan import LanScanner

    p = argparse.ArgumentParser(prog="python3 -m netpanel.backend.cameras",
                                description="Find Milesight / ONVIF cameras (same code as the NetPanel 'Cameras' screen).")
    p.add_argument("iface", nargs="?", default="eth0", help="interface to search on (default eth0)")
    p.add_argument("--time", type=float, default=DISCOVER_S, help="search time in seconds (default %(default)s)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--debug", action="store_true", help="print every datagram (hex + XML)")
    p.add_argument("--set-ip", metavar="MAC", help="change the IP of the Milesight camera with this MAC")
    p.add_argument("--ip"), p.add_argument("--mask", default="255.255.255.0"), p.add_argument("--gw")
    p.add_argument("--dns", default=""), p.add_argument("--port", type=int, default=0)
    p.add_argument("--user", default="admin")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(message)s")

    def dbg(proto, data, src):
        body, ok = ms_unframe(data) if proto == "milesight" else (data, None)
        msg = parse_ms(data) if proto == "milesight" else None
        if msg and msg["root"] == "Query":
            print("<- %s %s:%d  (a Query - our own via multicast loopback, or another Smart Tools)  Types=%s" % (
                proto, src[0], src[1], msg["fields"].get("Types", "?")), file=sys.stderr)
            return
        print("<- %s %s:%d  %d bytes  checksum=%s" % (proto, src[0], src[1], len(data), ok), file=sys.stderr)
        print(_hexdump(data[:96]) + ("\n  ..." if len(data) > 96 else ""), file=sys.stderr)
        print(_mask_secret(body.decode("utf-8", "replace")).strip(), file=sys.stderr)
        print(file=sys.stderr)

    lan = LanScanner()
    finder = CameraFinder(lan, debug=dbg if args.debug else None)
    nets = finder.local_networks()
    local = next((n for n in nets if n.iface == args.iface), None)
    if local is None:
        print("interface %s has no IPv4 network. directly connected: %s" % (
            args.iface, ", ".join("%s %s" % (n.iface, n.network) for n in nets) or "none"), file=sys.stderr)
        return 2
    if args.debug:
        print("-> query: %s" % ms_search_query(local.mac)[:-2].decode().replace("\r\n", " "), file=sys.stderr)

    mgr = CameraManager(finder, duration=args.time)
    print("Searching on %s (%s, %s) for %.0f s ..." % (local.iface, local.address, local.network, args.time), file=sys.stderr)
    mgr.start(local)
    mgr.wait(args.time + 15)
    snap = mgr.snapshot()

    if args.set_ip:
        cam = mgr.find(args.set_ip)
        if cam is None:
            print("camera %s not found" % args.set_ip, file=sys.stderr)
            return 1
        if not (args.ip and args.gw):
            print("--ip and --gw are required", file=sys.stderr)
            return 2
        password = getpass.getpass("Password for %s@%s: " % (args.user, cam.ip))
        cfg = {"address": args.ip, "netmask": args.mask, "gateway": args.gw, "dns": args.dns,
               "http_port": args.port or cam.http_port or 80}
        try:
            print(mgr.modify(cam.mac, local, cfg, args.user, password))
        except CameraError as e:
            print("ERROR: " + e.message, file=sys.stderr)
            return 1
        return 0

    if args.json:
        print(_json.dumps(snap, indent=2))
        return 0 if snap["state"] == "done" else 1
    if snap["state"] == "error":
        print("ERROR: %s" % snap["message"], file=sys.stderr)
        return 1
    rows = [("IP", "MAC", "TYPE", "MODEL", "NAME", "FIRMWARE", "VIA")]
    for c in snap["cams"]:
        flags = "" if c["in_subnet"] else " (other subnet)"
        if c["activated"] is False:
            flags += " (not activated)"
        rows.append((c["ip"] + flags, c["mac"] or "-", c["kind"], c["model"] or "-", c["name"] or "-",
                     c["firmware"] or "-", "+".join(c["sources"])))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(v.ljust(w[i]) for i, v in enumerate(r)).rstrip())
    print("%d device(s) in %.1f s" % (snap["count"], snap["duration"] or 0), file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
