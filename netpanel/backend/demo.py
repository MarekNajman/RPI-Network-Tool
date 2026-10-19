"""Simulated backend for development and screenshots (`--demo`).

Nothing on the host is touched. State lives in memory and operations sleep a
little so the UI's busy/rollback paths can be exercised on any PC.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import List

from ..models import APConfig, EthStatus, IPv4Config, StationRequest, TailscaleStatus, WifiNetwork, WifiStatus
from ..validation import netmask_from_prefix
from .nmparse import classify_security
from .runner import CommandError
from .tailscale import command_preview

_NETWORKS = [
    ("Office-WiFi", 78, "WPA2", 36, 5180),
    ("Workshop-2G", 64, "WPA1 WPA2", 6, 2437),
    ("Guest", 52, "", 11, 2462),
    ("Lab-WPA3", 47, "WPA3", 44, 5220),
    ("Production-Ent", 40, "WPA2 802.1X", 1, 2412),
    ("Printer-Direct", 31, "WPA2", 6, 2437),
    ("Meeting-Room", 22, "WPA2 WPA3", 48, 5240),
]


class DemoBackend:
    demo = True

    def __init__(self, eth: str = "eth0", wlan: str = "wlan0", delay: float = 1.2):
        self.delay = delay
        self._lock = threading.Lock()
        self.eth = EthStatus(
            iface=eth, state="connected", carrier=True, speed=1000, duplex="full",
            mac="dc:a6:32:4b:7e:01", connection="netpanel-eth", method="auto",
            address="192.168.10.57", prefix=24, netmask="255.255.255.0",
            gateway="192.168.10.1", dns=["192.168.10.1"],
            configured=IPv4Config(method="auto"),
        )
        self.wifi = WifiStatus(
            iface=wlan, regdomain="CZ", state="connected", mode="station",
            connection="netpanel-sta", ssid="Office-WiFi", signal=78, method="auto",
            address="10.20.30.41", prefix=24, netmask="255.255.255.0",
            gateway="10.20.30.1", dns=["10.20.30.1"], band="a", channel=36,
            mac="dc:a6:32:4b:7e:02",
        )
        self.ap = APConfig(ssid="NetPanel-AP", password="", band="bg", channel=6,
                           address="10.42.0.1", prefix=24, exists=False)
        self.ts = TailscaleStatus(
            installed=True, daemon=True, backend_state="Running",
            ips=["100.101.102.103", "fd7a:115c:a1e0::1"], hostname="netpanel",
            dns_name="netpanel.demo-tailnet.ts.net", tailnet="demo-tailnet.ts.net",
            online=True, advertised_routes=[], primary_routes=[], ip_forward=True,
        )

    def _sleep(self, factor: float = 1.0):
        time.sleep(self.delay * factor)

    # -------------------------------------------------------------- system
    def system(self) -> dict:
        return {"hostname": "netpanel-demo", "cpu_temp": 48.3, "uptime": 3600, "demo": True}

    # -------------------------------------------------------------- ethernet
    def eth_status(self) -> EthStatus:
        with self._lock:
            return copy.deepcopy(self.eth)

    def apply_eth(self, cfg: IPv4Config) -> str:
        self._sleep(1.5)
        with self._lock:
            self.eth.configured = copy.deepcopy(cfg)
            self.eth.method = cfg.method
            if cfg.method == "auto":
                self.eth.address, self.eth.prefix = "192.168.10.57", 24
                self.eth.gateway, self.eth.dns = "192.168.10.1", ["192.168.10.1"]
                msg = "Ethernet switched to DHCP."
            else:
                self.eth.address, self.eth.prefix = cfg.address, cfg.prefix
                self.eth.gateway, self.eth.dns = cfg.gateway, list(cfg.dns)
                msg = "Static IP %s/%d applied." % (cfg.address, cfg.prefix)
            self.eth.netmask = netmask_from_prefix(self.eth.prefix)
        return msg

    # -------------------------------------------------------------- wi-fi
    def wifi_status(self) -> WifiStatus:
        with self._lock:
            return copy.deepcopy(self.wifi)

    def wifi_scan(self) -> List[WifiNetwork]:
        self._sleep(1.0)
        with self._lock:
            if self.wifi.mode == "ap":
                raise CommandError("nmcli device wifi list", 1,
                                   message="Scanning is not possible while the access point is active")
            current = self.wifi.ssid if self.wifi.mode == "station" else ""
        out = []
        for ssid, sig, sec, ch, freq in _NETWORKS:
            out.append(WifiNetwork(ssid=ssid, signal=sig, security=sec, sec_kind=classify_security(sec),
                                   channel=ch, freq=freq, band="5 GHz" if freq > 4900 else "2.4 GHz",
                                   in_use=ssid == current))
        out.sort(key=lambda n: (not n.in_use, -n.signal))
        return out

    def wifi_set_radio(self, on: bool) -> str:
        self._sleep(0.5)
        with self._lock:
            self.wifi.radio = on
            if not on:
                self.wifi.mode, self.wifi.state, self.wifi.ssid = "none", "unavailable", ""
                self.wifi.address, self.wifi.prefix = "", None
        return "Wi-Fi radio turned %s." % ("on" if on else "off")

    def ap_config(self) -> APConfig:
        with self._lock:
            cfg = copy.deepcopy(self.ap)
            cfg.active = self.wifi.mode == "ap"
            return cfg

    def start_ap(self, cfg: APConfig) -> str:
        self._sleep(2.0)
        with self._lock:
            self.ap = copy.deepcopy(cfg)
            self.ap.exists = True
            self.wifi = WifiStatus(
                iface=self.wifi.iface, regdomain="CZ", state="connected", mode="ap",
                connection="netpanel-ap", ssid=cfg.ssid, method="shared",
                address=cfg.address, prefix=cfg.prefix, netmask=netmask_from_prefix(cfg.prefix),
                band=cfg.band, channel=cfg.channel, clients=0, mac=self.wifi.mac,
            )
        return "Access point '%s' is running on %s/%d." % (cfg.ssid, cfg.address, cfg.prefix)

    def stop_ap(self) -> str:
        self._sleep(1.0)
        with self._lock:
            self.wifi = WifiStatus(iface=self.wifi.iface, regdomain="CZ", state="disconnected",
                                   mode="none", mac=self.wifi.mac)
        return "Access point stopped. Wi-Fi is now in station mode."

    def connect_station(self, req: StationRequest) -> str:
        self._sleep(2.5)
        if req.password == "wrongpass":
            raise CommandError("nmcli connection up netpanel-sta", 4,
                               message="Could not connect to '%s': Secrets were required, but not provided. "
                                       "Wrong password? Previous connection restored." % req.ssid)
        with self._lock:
            ip = req.ipv4
            static = ip.method == "manual"
            self.wifi = WifiStatus(
                iface=self.wifi.iface, regdomain="CZ", state="connected", mode="station",
                connection="netpanel-sta", ssid=req.ssid, signal=70, method=ip.method,
                address=ip.address if static else "10.20.30.41",
                prefix=ip.prefix if static else 24,
                gateway=ip.gateway if static else "10.20.30.1",
                dns=list(ip.dns) if static else ["10.20.30.1"], mac=self.wifi.mac,
            )
            self.wifi.netmask = netmask_from_prefix(self.wifi.prefix)
        return "Connected to '%s'." % req.ssid

    # -------------------------------------------------------------- tailscale
    def ts_status(self) -> TailscaleStatus:
        with self._lock:
            return copy.deepcopy(self.ts)

    def ts_set_routes(self, routes: List[str]) -> dict:
        self._sleep(1.0)
        with self._lock:
            self.ts.advertised_routes = list(routes)
            # Demo: pretend the first route was approved earlier, new ones are pending.
            self.ts.primary_routes = [r for r in self.ts.primary_routes if r in routes]
        msg = ("Advertising %s. Approve the route in the Tailscale admin console." % ", ".join(routes)
               if routes else "Subnet advertising stopped.")
        return {"message": msg, "command": command_preview(routes)}
