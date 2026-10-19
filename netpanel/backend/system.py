"""Small system facts for the status bar."""

from __future__ import annotations

import socket
from typing import Optional


def hostname() -> str:
    return socket.gethostname()


def cpu_temp() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def uptime_s() -> Optional[int]:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None
