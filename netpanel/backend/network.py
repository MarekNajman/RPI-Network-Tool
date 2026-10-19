"""NetworkManager backend (nmcli).

NetPanel manages three dedicated NetworkManager profiles and leaves every other
profile on the system untouched:

  netpanel-eth  Ethernet, DHCP or static         autoconnect-priority 100
  netpanel-ap   Wi-Fi access point (shared/NAT)  autoconnect-priority 100
  netpanel-sta  Wi-Fi station (client)           autoconnect-priority 50

The highest autoconnect priority wins at boot, so the AP profile beats any
station profile while it has autoconnect enabled. Switching to station mode
disables autoconnect on the AP profile.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Dict, List, Optional

from ..models import APConfig, EthStatus, IPv4Config, StationRequest, WifiNetwork, WifiStatus
from ..validation import netmask_from_prefix
from . import nmparse as P
from .runner import CommandError, run, which

log = logging.getLogger("netpanel.nm")

ETH_PROFILE = "netpanel-eth"
AP_PROFILE = "netpanel-ap"
STA_PROFILE = "netpanel-sta"


class NetworkBackend:
    def __init__(self, eth: str = "eth0", wlan: str = "wlan0"):
        self.eth = eth
        self.wlan = wlan

    # ------------------------------------------------------------------ helpers

    def _nm(self, *args: str, timeout: float = 20, secrets=(), check=True, quiet=False) -> str:
        try:
            return run(["nmcli", *args], timeout=timeout, secrets=secrets, check=check, quiet=quiet).stdout
        except CommandError as e:
            low = e.message.lower()
            if "insufficient privileges" in low or "not authorized" in low:
                e.message = ("%s - NetworkManager refused the change for the 'netpanel' user (polkit). "
                             "Over SSH run: sudo /opt/netpanel/deploy/check-permissions.sh --fix" % e.message.rstrip("."))
            raise

    def _device_show(self, iface: str) -> Optional[Dict[str, str]]:
        try:
            out = self._nm("-e", "no", "-t", "-f", "GENERAL,IP4", "device", "show", iface, quiet=True)
        except CommandError as e:
            if "not found" in e.message.lower() or "no device" in e.message.lower():
                return None
            raise
        return P.parse_kv(out)

    def _profile(self, name: str, fields: str, secrets: bool = False) -> Dict[str, str]:
        args = ["-e", "no", "-t", "-f", fields, "connection", "show", name]
        if secrets:
            args.insert(0, "-s")
        return P.parse_kv(self._nm(*args, quiet=True))

    def _profile_exists(self, name: str) -> bool:
        out = self._nm("-t", "-f", "NAME", "connection", "show", quiet=True)
        return any(P.split_terse(line)[0] == name for line in out.splitlines())

    def _active_connection(self, iface: str) -> str:
        kv = self._device_show(iface) or {}
        return P.clean(kv.get("GENERAL.CONNECTION"))

    def _safe(self, *args: str, timeout: float = 30) -> bool:
        """Best-effort nmcli call used for rollbacks - never raises."""
        try:
            self._nm(*args, timeout=timeout)
            return True
        except CommandError as e:
            log.warning("rollback step failed: %s", e.message)
            return False

    @staticmethod
    def _sysfs(iface: str, attr: str) -> str:
        try:
            with open("/sys/class/net/%s/%s" % (iface, attr)) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _ipv4_props(cfg: IPv4Config, for_add: bool = False) -> List[str]:
        """nmcli property arguments. `connection modify` gets explicit empty
        values (to clear old static settings); `connection add` omits them."""
        if cfg.method == "auto":
            pairs = [("ipv4.method", "auto"), ("ipv4.addresses", ""),
                     ("ipv4.gateway", ""), ("ipv4.dns", "")]
        else:
            pairs = [("ipv4.method", "manual"),
                     ("ipv4.addresses", "%s/%d" % (cfg.address, cfg.prefix)),
                     ("ipv4.gateway", cfg.gateway or ""),
                     ("ipv4.dns", " ".join(cfg.dns))]
        out: List[str] = []
        for key, value in pairs:
            if for_add and value == "":
                continue
            out += [key, value]
        return out

    @staticmethod
    def _fill_ip(status, kv: Dict[str, str]) -> None:
        addrs = P.collect(kv, "IP4.ADDRESS")
        if addrs:
            status.address, status.prefix = P.split_cidr(addrs[0])
            status.netmask = netmask_from_prefix(status.prefix)
        status.gateway = P.clean(kv.get("IP4.GATEWAY"))
        status.dns = P.collect(kv, "IP4.DNS")

    def _read_ipv4_profile(self, name: str) -> Optional[IPv4Config]:
        try:
            kv = self._profile(name, "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns")
        except CommandError:
            return None
        method = P.clean(kv.get("ipv4.method")) or "auto"
        addrs = P.split_list(kv.get("ipv4.addresses"))
        address, prefix = P.split_cidr(addrs[0]) if addrs else ("", 24)
        return IPv4Config(
            method="manual" if method == "manual" else "auto",
            address=address,
            prefix=prefix or 24,
            gateway=P.clean(kv.get("ipv4.gateway")),
            dns=P.split_list(kv.get("ipv4.dns")),
        )

    # ------------------------------------------------------------------ Ethernet

    def eth_status(self) -> EthStatus:
        st = EthStatus(iface=self.eth)
        kv = self._device_show(self.eth)
        if kv is None:
            st.present = False
            st.state = "missing"
            return st
        st.state = P.state_name(kv.get("GENERAL.STATE"))
        st.mac = P.clean(kv.get("GENERAL.HWADDR")).lower()
        st.connection = P.clean(kv.get("GENERAL.CONNECTION"))
        st.carrier = self._sysfs(self.eth, "carrier") == "1"
        speed = self._sysfs(self.eth, "speed")
        st.speed = int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None
        st.duplex = self._sysfs(self.eth, "duplex") if st.carrier else ""
        self._fill_ip(st, kv)

        profile = st.connection or (ETH_PROFILE if self._profile_exists(ETH_PROFILE) else "")
        if profile:
            st.configured = self._read_ipv4_profile(profile)
            if st.configured:
                st.method = st.configured.method
        return st

    def apply_eth(self, cfg: IPv4Config) -> str:
        common = [
            "connection.interface-name", self.eth,
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "100",
        ]
        if self._profile_exists(ETH_PROFILE):
            self._nm("connection", "modify", ETH_PROFILE, *common, *self._ipv4_props(cfg))
        else:
            self._nm("connection", "add", "type", "ethernet", "ifname", self.eth,
                     "con-name", ETH_PROFILE, *common, "ipv6.method", "auto",
                     *self._ipv4_props(cfg, for_add=True))

        if self._sysfs(self.eth, "carrier") != "1":
            return "Saved. The cable is unplugged - settings apply when the link comes up."
        try:
            self._nm("--wait", "60", "connection", "up", ETH_PROFILE, timeout=70)
        except CommandError as e:
            if "timeout" in e.message.lower() and cfg.method == "auto":
                e.message = ("Settings saved, but no DHCP lease within 60 s. NetworkManager keeps "
                             "trying - check the DHCP server or switch to a static IP.")
            raise
        if cfg.method == "auto":
            return "Ethernet switched to DHCP."
        return "Static IP %s/%d applied." % (cfg.address, cfg.prefix)

    # ------------------------------------------------------------------ Wi-Fi status

    def _radio(self):
        try:
            out = self._nm("-t", "-f", "WIFI-HW,WIFI", "general", "status", quiet=True).strip()
            hw, sw = (P.split_terse(out) + ["", ""])[:2]
            return hw == "enabled", sw == "enabled"
        except CommandError:
            return True, True

    def _regdomain(self) -> str:
        if not which("iw"):
            return ""
        try:
            out = run(["iw", "reg", "get"], timeout=5, quiet=True).stdout
        except CommandError:
            return ""
        m = re.search(r"country (\w\w):", out)
        return m.group(1) if m else ""

    def _ap_clients(self) -> Optional[int]:
        if not which("iw"):
            return None
        try:
            out = run(["iw", "dev", self.wlan, "station", "dump"], timeout=5, quiet=True).stdout
        except CommandError:
            return None
        return sum(1 for line in out.splitlines() if line.startswith("Station "))

    def wifi_status(self) -> WifiStatus:
        st = WifiStatus(iface=self.wlan)
        st.radio_hw, st.radio = self._radio()
        st.regdomain = self._regdomain()
        kv = self._device_show(self.wlan)
        if kv is None:
            st.present = False
            st.state = "missing"
            return st
        st.state = P.state_name(kv.get("GENERAL.STATE"))
        st.mac = P.clean(kv.get("GENERAL.HWADDR")).lower()
        st.connection = P.clean(kv.get("GENERAL.CONNECTION"))
        self._fill_ip(st, kv)
        if not st.connection:
            return st
        try:
            prof = self._profile(
                st.connection,
                "802-11-wireless.mode,802-11-wireless.ssid,802-11-wireless.band,"
                "802-11-wireless.channel,ipv4.method",
            )
        except CommandError:
            return st
        mode = P.clean(prof.get("802-11-wireless.mode"))
        st.ssid = P.raw(prof.get("802-11-wireless.ssid"))
        st.method = "manual" if P.clean(prof.get("ipv4.method")) == "manual" else "auto"
        if mode == "ap":
            st.mode = "ap"
            st.band = P.clean(prof.get("802-11-wireless.band"))
            ch = P.clean(prof.get("802-11-wireless.channel"))
            st.channel = int(ch) if ch.isdigit() else None
            st.clients = self._ap_clients()
        else:
            st.mode = "station"
            for net in self._wifi_list(rescan="no"):
                if net.in_use:
                    st.signal = net.signal
                    st.channel = net.channel
                    st.band = "a" if net.freq >= 4900 else "bg"
                    break
        return st

    # ------------------------------------------------------------------ scanning

    def _wifi_list(self, rescan: str) -> List[WifiNetwork]:
        out = self._nm(
            "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN,FREQ",
            "device", "wifi", "list", "ifname", self.wlan, "--rescan", rescan,
            timeout=40, quiet=rescan == "no",
        )
        best: Dict[str, WifiNetwork] = {}
        for line in out.splitlines():
            f = P.split_terse(line)
            if len(f) < 6:
                continue
            in_use, ssid, signal, sec, chan, freq = f[:6]
            if not ssid:
                continue  # hidden networks are entered manually
            net = WifiNetwork(
                ssid=ssid,
                signal=int(signal) if signal.isdigit() else 0,
                security=P.clean(sec),
                sec_kind=P.classify_security(sec),
                channel=int(chan) if chan.isdigit() else 0,
                freq=P.parse_freq(freq),
                in_use=in_use.strip() == "*",
            )
            net.band = "5 GHz" if net.freq >= 4900 else "2.4 GHz"
            cur = best.get(ssid)
            if cur is None or net.in_use or (not cur.in_use and net.signal > cur.signal):
                best[ssid] = net
        return sorted(best.values(), key=lambda n: (not n.in_use, -n.signal, n.ssid.lower()))

    def wifi_scan(self) -> List[WifiNetwork]:
        try:
            return self._wifi_list(rescan="yes")
        except CommandError as e:
            # NM refuses back-to-back rescans; fall back to its cached list.
            if "not allowed" in e.message.lower() or "scan" in e.message.lower():
                return self._wifi_list(rescan="auto")
            raise

    def wifi_set_radio(self, on: bool) -> str:
        self._nm("radio", "wifi", "on" if on else "off")
        return "Wi-Fi radio turned %s." % ("on" if on else "off")

    # ------------------------------------------------------------------ access point

    def ap_config(self) -> APConfig:
        if not self._profile_exists(AP_PROFILE):
            return APConfig(exists=False)
        fields = ("802-11-wireless.ssid,802-11-wireless.band,802-11-wireless.channel,"
                  "802-11-wireless-security.psk,ipv4.addresses")
        try:
            kv = self._profile(AP_PROFILE, fields, secrets=True)
        except CommandError:
            kv = self._profile(AP_PROFILE, fields.replace("802-11-wireless-security.psk,", ""))
        addrs = P.split_list(kv.get("ipv4.addresses"))
        address, prefix = P.split_cidr(addrs[0]) if addrs else ("10.42.0.1", 24)
        ch = P.clean(kv.get("802-11-wireless.channel"))
        band = P.clean(kv.get("802-11-wireless.band")) or "bg"
        return APConfig(
            ssid=P.raw(kv.get("802-11-wireless.ssid")),
            password=P.raw(kv.get("802-11-wireless-security.psk")),
            band=band,
            channel=int(ch) if ch.isdigit() and int(ch) > 0 else (6 if band == "bg" else 36),
            address=address or "10.42.0.1",
            prefix=prefix or 24,
            exists=True,
            active=self._active_connection(self.wlan) == AP_PROFILE,
        )

    # Changes to the AP and station profiles are staged in a temporary profile
    # ("<name>-new"). The working profile is replaced only after the new one has
    # come up, so a failed change never destroys the previous configuration.

    def _restore(self, prev: str) -> str:
        if not prev:
            return ""
        if self._safe("--wait", "40", "connection", "up", prev, timeout=50):
            return " Previous connection '%s' restored." % prev
        return " WARNING: previous connection '%s' could not be restored." % prev

    def _promote(self, staged: str, final: str) -> None:
        if self._profile_exists(final):
            self._nm("connection", "delete", final)
        self._nm("connection", "modify", staged, "connection.id", final)

    def _drop_staged(self, staged: str) -> None:
        if self._profile_exists(staged):
            self._safe("connection", "delete", staged)

    def start_ap(self, cfg: APConfig) -> str:
        prev = self._active_connection(self.wlan)
        staged = AP_PROFILE + "-new"
        props = [
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "100",
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", cfg.band,
            "802-11-wireless.channel", str(cfg.channel),
            "ipv4.method", "shared",  # DHCP server + NAT via NetworkManager's dnsmasq
            "ipv4.addresses", "%s/%d" % (cfg.address, cfg.prefix),
            "ipv6.method", "disabled",
            "802-11-wireless-security.key-mgmt", "wpa-psk",
            "802-11-wireless-security.psk", cfg.password,
            "802-11-wireless-security.proto", "rsn",
            "802-11-wireless-security.pairwise", "ccmp",
            "802-11-wireless-security.group", "ccmp",
            # brcmfmac (Pi 4 Wi-Fi) + PMF: many clients fail to join; WPA2 does not need it.
            "802-11-wireless-security.pmf", "disable",
        ]
        self._drop_staged(staged)
        self._nm("connection", "add", "type", "wifi", "ifname", self.wlan, "con-name", staged,
                 "ssid", cfg.ssid, *props, secrets=[cfg.password])
        try:
            self._nm("--wait", "40", "connection", "up", staged, timeout=50)
        except CommandError as e:
            self._drop_staged(staged)
            e.message = "Access point failed to start: %s.%s" % (e.message.rstrip("."), self._restore(prev))
            raise
        self._promote(staged, AP_PROFILE)
        return "Access point '%s' is running on %s/%d." % (cfg.ssid, cfg.address, cfg.prefix)

    def stop_ap(self) -> str:
        if not self._profile_exists(AP_PROFILE):
            return "No access point configured."
        self._nm("connection", "modify", AP_PROFILE, "connection.autoconnect", "no")
        if self._active_connection(self.wlan) == AP_PROFILE:
            self._nm("--wait", "20", "connection", "down", AP_PROFILE, timeout=30)
        return "Access point stopped. Wi-Fi is now in station mode."

    # ------------------------------------------------------------------ station

    def connect_station(self, req: StationRequest) -> str:
        prev = self._active_connection(self.wlan)
        staged = STA_PROFILE + "-new"
        ap_autoconnect = False
        if self._profile_exists(AP_PROFILE):
            try:
                kv = self._profile(AP_PROFILE, "connection.autoconnect")
                ap_autoconnect = P.clean(kv.get("connection.autoconnect")) == "yes"
            except CommandError:
                pass

        args = [
            "connection", "add", "type", "wifi", "ifname", self.wlan,
            "con-name", staged, "ssid", req.ssid,
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "50",
            "802-11-wireless.mode", "infrastructure",
        ]
        if req.hidden:
            args += ["802-11-wireless.hidden", "yes"]
        if req.security in ("wpa-psk", "sae"):
            args += ["802-11-wireless-security.key-mgmt", req.security,
                     "802-11-wireless-security.psk", req.password]
        args += self._ipv4_props(req.ipv4, for_add=True)
        self._drop_staged(staged)
        self._nm(*args, secrets=[req.password])

        # Station mode wins over the AP at the next boot.
        if ap_autoconnect:
            self._nm("connection", "modify", AP_PROFILE, "connection.autoconnect", "no")
        try:
            self._nm("--wait", "60", "connection", "up", staged, timeout=75)
        except CommandError as e:
            # Roll back: drop the new profile, the previous one is untouched.
            self._drop_staged(staged)
            if ap_autoconnect:
                self._safe("connection", "modify", AP_PROFILE, "connection.autoconnect", "yes")
            hint = " Wrong password?" if "secrets were required" in e.message.lower() else ""
            e.message = "Could not connect to '%s': %s.%s%s" % (
                req.ssid, e.message.rstrip("."), hint, self._restore(prev))
            raise
        self._promote(staged, STA_PROFILE)
        return "Connected to '%s'." % req.ssid


def eth_network(status: EthStatus) -> Optional[ipaddress.IPv4Network]:
    """Network of the Ethernet interface (live address, else configured static)."""
    addr, prefix = status.address, status.prefix
    if not addr and status.configured and status.configured.method == "manual":
        addr, prefix = status.configured.address, status.configured.prefix
    if not addr or prefix is None:
        return None
    try:
        return ipaddress.IPv4Network("%s/%d" % (addr, prefix), strict=False)
    except ValueError:
        return None


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0
