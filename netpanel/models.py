"""Plain data structures shared by backends and the HTTP API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class IPv4Config:
    """IPv4 settings of a NetworkManager profile."""

    method: str = "auto"  # "auto" = DHCP, "manual" = static
    address: str = ""
    prefix: int = 24
    gateway: str = ""
    dns: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "IPv4Config":
        d = d or {}
        dns = d.get("dns") or []
        if isinstance(dns, str):
            dns = [x.strip() for x in dns.replace(";", ",").split(",") if x.strip()]
        try:
            prefix = int(d.get("prefix") or 24)
        except (TypeError, ValueError):
            prefix = -1  # rejected later by validation
        return cls(
            method=str(d.get("method") or "auto"),
            address=str(d.get("address") or "").strip(),
            prefix=prefix,
            gateway=str(d.get("gateway") or "").strip(),
            dns=[str(x).strip() for x in dns],
        )


@dataclass
class EthStatus:
    iface: str = "eth0"
    present: bool = True
    state: str = "unknown"  # connected/connecting/disconnected/unavailable/unmanaged/missing
    carrier: bool = False
    speed: Optional[int] = None  # Mb/s
    duplex: str = ""
    mac: str = ""
    connection: str = ""  # active NetworkManager profile
    method: Optional[str] = None  # method of the active profile: auto/manual
    address: str = ""
    prefix: Optional[int] = None
    netmask: str = ""
    gateway: str = ""
    dns: List[str] = field(default_factory=list)
    configured: Optional[IPv4Config] = None  # IPv4 config stored in the profile


@dataclass
class WifiNetwork:
    ssid: str
    signal: int = 0  # 0-100 %
    security: str = ""  # raw nmcli value, e.g. "WPA2", "WPA2 WPA3", ""
    sec_kind: str = "open"  # open / wpa-psk / sae / enterprise / wep / owe / unsupported
    channel: int = 0
    freq: int = 0  # MHz
    band: str = ""  # "2.4 GHz" / "5 GHz"
    in_use: bool = False


@dataclass
class WifiStatus:
    iface: str = "wlan0"
    present: bool = True
    radio: bool = True  # software switch (nmcli radio wifi)
    radio_hw: bool = True  # hardware switch / rfkill
    regdomain: str = ""  # e.g. "CZ", "00" = not set
    state: str = "unknown"
    mode: str = "none"  # "station" / "ap" / "none"
    connection: str = ""
    ssid: str = ""
    signal: Optional[int] = None
    method: Optional[str] = None
    address: str = ""
    prefix: Optional[int] = None
    netmask: str = ""
    gateway: str = ""
    dns: List[str] = field(default_factory=list)
    band: str = ""  # "bg" / "a" (AP mode)
    channel: Optional[int] = None
    clients: Optional[int] = None  # AP mode: associated stations
    mac: str = ""


@dataclass
class APConfig:
    ssid: str = ""
    password: str = ""
    band: str = "bg"  # "bg" = 2.4 GHz, "a" = 5 GHz
    channel: int = 6
    address: str = "10.42.0.1"
    prefix: int = 24
    exists: bool = False  # profile already stored in NetworkManager
    active: bool = False

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "APConfig":
        d = d or {}

        def _int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return cls(
            ssid=str(d.get("ssid") or ""),
            password=str(d.get("password") or ""),
            band=str(d.get("band") or "bg"),
            channel=_int(d.get("channel"), -1),
            address=str(d.get("address") or "").strip(),
            prefix=_int(d.get("prefix"), -1),
        )


@dataclass
class StationRequest:
    ssid: str
    security: str = "wpa-psk"  # open / wpa-psk / sae
    password: str = ""
    hidden: bool = False
    ipv4: IPv4Config = field(default_factory=IPv4Config)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "StationRequest":
        d = d or {}
        return cls(
            ssid=str(d.get("ssid") or ""),
            security=str(d.get("security") or "wpa-psk"),
            password=str(d.get("password") or ""),
            hidden=bool(d.get("hidden")),
            ipv4=IPv4Config.from_dict(d.get("ipv4")),
        )


@dataclass
class TailscaleStatus:
    installed: bool = True
    daemon: bool = False  # tailscaled reachable
    backend_state: str = ""  # Running / NeedsLogin / Stopped / Starting / NoState
    ips: List[str] = field(default_factory=list)
    hostname: str = ""
    dns_name: str = ""
    tailnet: str = ""
    online: Optional[bool] = None
    advertised_routes: Optional[List[str]] = None  # None = unknown
    primary_routes: List[str] = field(default_factory=list)  # approved & served by this node
    ip_forward: Optional[bool] = None  # net.ipv4.ip_forward (required for subnet routing)
    error: str = ""


@dataclass
class ScanHost:
    """One device found by the LAN scan."""

    ip: str
    mac: str = ""
    vendor: str = ""          # shortened organisation name ("Raspberry Pi Trading")
    vendor_full: str = ""     # as registered at the IEEE
    random_mac: bool = False  # locally administered ("private") MAC - no vendor exists
    name: str = ""
    name_source: str = ""     # dhcp / mdns / netbios / dns / this device
    names: Dict[str, str] = field(default_factory=dict)  # every name found, by source
    tags: List[str] = field(default_factory=list)        # "self", "gateway"

    def to_json(self) -> dict:
        return asdict(self)


def to_json(obj):
    """Convert dataclasses (possibly nested in lists/dicts) into JSON-ready data."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [to_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    return obj
