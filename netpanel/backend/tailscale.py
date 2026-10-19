"""Tailscale backend: read status (unprivileged) and set advertised routes (sudo)."""

from __future__ import annotations

import json
import logging
import os
from typing import List

from ..models import TailscaleStatus
from .runner import CommandError, run, which

log = logging.getLogger("netpanel.tailscale")

# Root-owned helper allowed via /etc/sudoers.d/netpanel. It re-validates the
# CIDR list and then runs `tailscale set --advertise-routes=<list>`.
HELPER = "/usr/local/sbin/netpanel-ts-routes"


def _read_ip_forward():
    try:
        with open("/proc/sys/net/ipv4/ip_forward") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def command_preview(routes: List[str]) -> str:
    return "sudo tailscale set --advertise-routes=%s" % ",".join(routes)


class TailscaleBackend:
    def status(self) -> TailscaleStatus:
        st = TailscaleStatus(ip_forward=_read_ip_forward())
        if not which("tailscale"):
            st.installed = False
            st.error = "Tailscale is not installed"
            return st
        proc = run(["tailscale", "status", "--json"], timeout=10, check=False, quiet=True)
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            data = None
        if not data:
            st.daemon = False
            err = (proc.stderr or proc.stdout).strip().splitlines()
            st.error = err[-1] if err else "tailscaled is not running"
            return st

        st.daemon = True
        st.backend_state = data.get("BackendState") or ""
        me = data.get("Self") or {}
        st.ips = list(me.get("TailscaleIPs") or data.get("TailscaleIPs") or [])
        st.hostname = me.get("HostName") or ""
        st.dns_name = (me.get("DNSName") or "").rstrip(".")
        st.online = me.get("Online")
        st.primary_routes = list(me.get("PrimaryRoutes") or [])
        st.tailnet = (data.get("CurrentTailnet") or {}).get("Name") or ""

        prefs = run(["tailscale", "debug", "prefs"], timeout=10, check=False, quiet=True)
        if prefs.returncode == 0:
            try:
                routes = json.loads(prefs.stdout).get("AdvertiseRoutes") or []
                # 0.0.0.0/0 and ::/0 mean "exit node" - not a subnet route.
                st.advertised_routes = [r for r in routes if r not in ("0.0.0.0/0", "::/0")]
            except ValueError:
                st.advertised_routes = None
        return st

    def set_routes(self, routes: List[str]) -> dict:
        value = ",".join(routes)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd = ["tailscale", "set", "--advertise-routes=" + value]
        elif os.path.exists(HELPER):
            cmd = ["sudo", "-n", HELPER, value or "--clear"]
        else:
            cmd = ["sudo", "-n", "tailscale", "set", "--advertise-routes=" + value]
        try:
            run(cmd, timeout=40)
        except CommandError as e:
            text = (e.stderr or e.message).lower()
            if "sudo:" in text and ("password is required" in text or "may not run" in text
                                    or "not allowed to execute" in text):
                e.message = ("Permission denied for 'tailscale set'. The sudoers rule "
                             "/etc/sudoers.d/netpanel is missing - run deploy/install.sh.")
            raise
        if routes:
            msg = "Advertising %s. Approve the route in the Tailscale admin console." % ", ".join(routes)
        else:
            msg = "Subnet advertising stopped."
        return {"message": msg, "command": command_preview(routes)}
