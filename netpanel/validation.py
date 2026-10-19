"""Input validation shared by the HTTP API (authoritative) and unit tests.

The web UI performs the same checks for instant feedback, but the server never
trusts the browser: every value is validated here before it reaches nmcli or
tailscale.
"""

from __future__ import annotations

import ipaddress
from typing import Dict, Iterable, List, Optional, Tuple

from .models import APConfig, IPv4Config, StationRequest

CHANNELS_24 = list(range(1, 14))  # EU: 1-13
# 5 GHz access point: only the non-DFS UNII-1 channels. DFS channels need radar
# detection, which the Raspberry Pi Wi-Fi chip does not support in AP mode.
CHANNELS_5 = [36, 40, 44, 48]

PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

Errors = Dict[str, str]


class ValidationError(ValueError):
    def __init__(self, errors: Errors, message: str = "Please fix the highlighted fields."):
        super().__init__(message)
        self.errors = errors
        self.message = message


# --------------------------------------------------------------------------- basics


def parse_ipv4(text: str) -> ipaddress.IPv4Address:
    """Strict dotted-quad parser (rejects leading zeros, IPv6, hostnames)."""
    text = (text or "").strip()
    if text.count(".") != 3:
        raise ValueError("Enter an IPv4 address like 192.168.1.10")
    try:
        return ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        raise ValueError("Enter an IPv4 address like 192.168.1.10") from None


def parse_prefix(text) -> int:
    """Accept '24', '/24' or '255.255.255.0' and return the prefix length."""
    if isinstance(text, int):
        p = text
    else:
        s = str(text or "").strip().lstrip("/")
        if not s:
            raise ValueError("Enter a mask like 255.255.255.0 or /24")
        if "." in s:
            try:
                net = ipaddress.IPv4Network("0.0.0.0/" + s)
            except ValueError:
                raise ValueError("Invalid subnet mask") from None
            # ipaddress also accepts host masks (0.0.0.255) - we do not.
            if str(net.netmask) != s:
                raise ValueError("Invalid subnet mask")
            return net.prefixlen
        if not s.isdigit():
            raise ValueError("Enter a mask like 255.255.255.0 or /24")
        p = int(s)
    if not 0 <= p <= 32:
        raise ValueError("Prefix must be between /0 and /32")
    return p


def netmask_from_prefix(prefix: Optional[int]) -> str:
    if prefix is None or not 0 <= int(prefix) <= 32:
        return ""
    return str(ipaddress.IPv4Network("0.0.0.0/%d" % int(prefix)).netmask)


def network_of(address: str, prefix: int) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network("%s/%d" % (address, prefix), strict=False)


def _host_address_problem(ip: ipaddress.IPv4Address) -> Optional[str]:
    if ip.is_unspecified:
        return "0.0.0.0 is not a valid host address"
    if ip.is_loopback:
        return "Loopback addresses (127.x) are not allowed"
    if ip.is_multicast:
        return "Multicast addresses are not allowed"
    if ip.is_link_local:
        return "Link-local addresses (169.254.x) are not allowed"
    if ip.is_reserved or str(ip) == "255.255.255.255":
        return "Reserved address"
    return None


# --------------------------------------------------------------------------- IPv4 config


def validate_ipv4_config(cfg: IPv4Config, require_gateway: bool = False) -> IPv4Config:
    """Validate and normalise an IPv4 config. Raises ValidationError."""
    if cfg.method not in ("auto", "manual"):
        raise ValidationError({"method": "Unknown mode"})
    if cfg.method == "auto":
        return IPv4Config(method="auto")

    errors: Errors = {}
    ip = None
    try:
        ip = parse_ipv4(cfg.address)
        problem = _host_address_problem(ip)
        if problem:
            errors["address"] = problem
    except ValueError as e:
        errors["address"] = str(e)

    prefix = None
    try:
        prefix = parse_prefix(cfg.prefix)
        if prefix < 1:
            errors["prefix"] = "Prefix /0 is not usable"
            prefix = None
    except ValueError as e:
        errors["prefix"] = str(e)

    net = None
    if ip is not None and prefix is not None and "address" not in errors:
        net = network_of(str(ip), prefix)
        if prefix <= 30 and ip in (net.network_address, net.broadcast_address):
            errors["address"] = "This is the network/broadcast address of %s" % net

    gw_str = ""
    if cfg.gateway:
        try:
            gw = parse_ipv4(cfg.gateway)
            if net is not None and gw not in net:
                errors["gateway"] = "Gateway must be inside %s" % net
            elif ip is not None and gw == ip:
                errors["gateway"] = "Gateway cannot be the device's own address"
            elif _host_address_problem(gw):
                errors["gateway"] = _host_address_problem(gw)
            gw_str = str(gw)
        except ValueError as e:
            errors["gateway"] = str(e)
    elif require_gateway:
        errors["gateway"] = "Gateway is required"

    dns_out: List[str] = []
    if len(cfg.dns) > 3:
        errors["dns"] = "At most 3 DNS servers"
    for d in cfg.dns:
        if not d:
            continue
        try:
            dip = parse_ipv4(d)
            if dip.is_unspecified or dip.is_multicast:
                raise ValueError("Invalid DNS server %s" % d)
            dns_out.append(str(dip))
        except ValueError:
            errors["dns"] = "Invalid DNS server: %s" % d
            break

    if errors:
        raise ValidationError(errors)
    return IPv4Config(method="manual", address=str(ip), prefix=prefix, gateway=gw_str, dns=dns_out)


# --------------------------------------------------------------------------- Wi-Fi


def validate_ssid(ssid: str) -> Optional[str]:
    n = len((ssid or "").encode("utf-8"))
    if n == 0:
        return "SSID cannot be empty"
    if n > 32:
        return "SSID is longer than 32 bytes"
    return None


def validate_psk(psk: str) -> Optional[str]:
    psk = psk or ""
    if len(psk) == 64 and all(c in "0123456789abcdefABCDEF" for c in psk):
        return None
    if not 8 <= len(psk) <= 63:
        return "Password must have 8-63 characters"
    if any(not (0x20 <= ord(c) <= 0x7E) for c in psk):
        return "Password may contain only ASCII characters"
    return None


def allowed_channels(band: str, regdomain: Optional[str]) -> List[int]:
    """AP channels usable for the Wi-Fi country. Unknown/unset country ("00")
    only gets the world-safe 2.4 GHz channels 1-11 and no 5 GHz."""
    rd = (regdomain or "").upper()
    if band == "bg":
        return list(range(1, 12)) if rd in ("", "00", "US", "CA", "TW") else CHANNELS_24
    return [] if rd in ("", "00") else CHANNELS_5


def validate_ap(cfg: APConfig, avoid_networks: Iterable[ipaddress.IPv4Network] = (),
                regdomain: Optional[str] = None) -> APConfig:
    """`regdomain=None` skips the country check (unknown), "" / "00" = not set."""
    errors: Errors = {}
    e = validate_ssid(cfg.ssid)
    if e:
        errors["ssid"] = e
    e = validate_psk(cfg.password)
    if e:
        errors["password"] = e
    if cfg.band not in ("bg", "a"):
        errors["band"] = "Band must be 2.4 GHz or 5 GHz"
    else:
        allowed = CHANNELS_24 if cfg.band == "bg" else CHANNELS_5
        if regdomain is not None:
            allowed = allowed_channels(cfg.band, regdomain)
        if not allowed:
            errors["band"] = "5 GHz needs the Wi-Fi country set (raspi-config -> WLAN Country)"
        elif cfg.channel not in allowed:
            errors["channel"] = "Channel %s is not allowed on %s" % (
                cfg.channel,
                "2.4 GHz" if cfg.band == "bg" else "5 GHz",
            )

    ip = None
    try:
        ip = parse_ipv4(cfg.address)
        if not any(ip in n for n in PRIVATE_NETS):
            errors["address"] = "Use a private address (10.x, 172.16-31.x, 192.168.x)"
    except ValueError as ex:
        errors["address"] = str(ex)
    prefix = cfg.prefix
    if not isinstance(prefix, int) or not 16 <= prefix <= 30:
        errors["prefix"] = "AP prefix must be between /16 and /30"
    if ip is not None and "address" not in errors and "prefix" not in errors:
        net = network_of(str(ip), prefix)
        if ip in (net.network_address, net.broadcast_address):
            errors["address"] = "Use a host address, e.g. %s" % next(net.hosts())
        else:
            for other in avoid_networks:
                if other is not None and net.overlaps(other):
                    errors["address"] = "AP subnet %s overlaps Ethernet subnet %s" % (net, other)
                    break
    if errors:
        raise ValidationError(errors)
    return APConfig(
        ssid=cfg.ssid,
        password=cfg.password,
        band=cfg.band,
        channel=cfg.channel,
        address=str(ip),
        prefix=prefix,
    )


def validate_station(req: StationRequest) -> StationRequest:
    errors: Errors = {}
    e = validate_ssid(req.ssid)
    if e:
        errors["ssid"] = e
    if req.security not in ("open", "wpa-psk", "sae"):
        errors["security"] = "Unsupported security type"
    elif req.security != "open":
        e = validate_psk(req.password)
        if e:
            errors["password"] = e
    ipv4 = req.ipv4
    try:
        ipv4 = validate_ipv4_config(req.ipv4)
    except ValidationError as ve:
        errors.update(ve.errors)
    if errors:
        raise ValidationError(errors)
    return StationRequest(
        ssid=req.ssid,
        security=req.security,
        password=req.password if req.security != "open" else "",
        hidden=req.hidden,
        ipv4=ipv4,
    )


# --------------------------------------------------------------------------- Tailscale


def canonical_route(network: str, prefix) -> Tuple[str, bool]:
    """Return ("192.168.1.0/24", host_bits_were_set). Raises ValidationError."""
    errors: Errors = {}
    ip = None
    p = None
    try:
        ip = parse_ipv4(network)
    except ValueError as e:
        errors["network"] = str(e)
    try:
        p = parse_prefix(prefix)
        if p < 1:
            errors["prefix"] = "Use --advertise-exit-node instead of /0"
    except ValueError as e:
        errors["prefix"] = str(e)
    if errors:
        raise ValidationError(errors)
    net = network_of(str(ip), p)
    if net.network_address.is_loopback or net.network_address.is_multicast:
        raise ValidationError({"network": "Loopback/multicast ranges cannot be advertised"})
    if net.network_address.is_link_local:
        raise ValidationError({"network": "Link-local range cannot be advertised"})
    if net.overlaps(TAILSCALE_CGNAT):
        raise ValidationError({"network": "Overlaps the Tailscale range 100.64.0.0/10"})
    return str(net), net.network_address != ip


def validate_routes(routes: Iterable[str]) -> List[str]:
    """Validate a list of CIDR strings (each must already be canonical)."""
    out: List[str] = []
    for r in routes:
        r = str(r).strip()
        if "/" not in r:
            raise ValidationError({"network": "Route %s needs a prefix, e.g. /24" % r})
        addr, _, pfx = r.partition("/")
        canon, changed = canonical_route(addr, pfx)
        if changed:
            raise ValidationError({"network": "%s has host bits set; use %s" % (r, canon)})
        if canon not in out:
            out.append(canon)
    return out
