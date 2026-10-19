"""Backend facade used by the HTTP API.

`Backend` talks to the real system (nmcli, tailscale, sysfs).
`DemoBackend` (demo.py) simulates everything for development on a PC.
"""

from __future__ import annotations

from typing import List

from ..models import APConfig, EthStatus, IPv4Config, StationRequest, TailscaleStatus, WifiNetwork, WifiStatus
from . import system
from .network import NetworkBackend
from .tailscale import TailscaleBackend


class Backend:
    demo = False

    def __init__(self, eth: str = "eth0", wlan: str = "wlan0"):
        self.net = NetworkBackend(eth=eth, wlan=wlan)
        self.ts = TailscaleBackend()

    def system(self) -> dict:
        return {"hostname": system.hostname(), "cpu_temp": system.cpu_temp(), "uptime": system.uptime_s(), "demo": False}

    def eth_status(self) -> EthStatus:
        return self.net.eth_status()

    def apply_eth(self, cfg: IPv4Config) -> str:
        return self.net.apply_eth(cfg)

    def wifi_status(self) -> WifiStatus:
        return self.net.wifi_status()

    def wifi_scan(self) -> List[WifiNetwork]:
        return self.net.wifi_scan()

    def wifi_set_radio(self, on: bool) -> str:
        return self.net.wifi_set_radio(on)

    def ap_config(self) -> APConfig:
        return self.net.ap_config()

    def start_ap(self, cfg: APConfig) -> str:
        return self.net.start_ap(cfg)

    def stop_ap(self) -> str:
        return self.net.stop_ap()

    def connect_station(self, req: StationRequest) -> str:
        return self.net.connect_station(req)

    def ts_status(self) -> TailscaleStatus:
        return self.ts.status()

    def ts_set_routes(self, routes: List[str]) -> dict:
        return self.ts.set_routes(routes)
