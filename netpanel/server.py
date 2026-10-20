"""HTTP server: JSON API under /api/ and the static web UI.

Security model (this is a single-purpose kiosk appliance):
  * listens on 127.0.0.1 only by default - not reachable from the network;
  * rejects requests whose Host header is not localhost (DNS rebinding);
  * POST requests must carry `X-NetPanel: 1` and a JSON body, which a foreign
    web page cannot send cross-origin without a CORS preflight (never granted);
  * strict Content-Security-Policy, no inline scripts;
  * everything except the read-only overview (/api/status) and the login
    endpoints requires an admin session (PIN login, header X-NetPanel-Token).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import secrets
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__
from .auth import AuthError, AuthManager
from .backend.lanscan import ScanBusy, suggest_range
from .backend.network import eth_network
from .backend.runner import CommandError
from .models import APConfig, IPv4Config, StationRequest, to_json
from .validation import (
    ValidationError,
    canonical_route,
    validate_ap,
    validate_ipv4_config,
    validate_routes,
    validate_scan_range,
    validate_station,
)

log = logging.getLogger("netpanel.http")

MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class Busy(Exception):
    pass


def _gen_password(n: int = 10) -> str:
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0OolI1")
    return "".join(secrets.choice(alphabet) for _ in range(n))


class Api:
    """Request handlers. Each returns (http_status, json_payload)."""

    def __init__(self, backend, auth: AuthManager):
        self.b = backend
        self.auth = auth
        self._write_lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, object]] = {}
        self._cache_lock = threading.Lock()
        self._ap_default_password = _gen_password()  # stable suggestion until an AP is saved

    # ------------------------------------------------------------- helpers

    def _cached(self, key: str, ttl: float, fn: Callable):
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def _invalidate(self):
        with self._cache_lock:
            self._cache.clear()

    def _write(self, fn: Callable, *args):
        if not self._write_lock.acquire(blocking=False):
            raise Busy()
        try:
            return fn(*args)
        finally:
            self._invalidate()
            self._write_lock.release()

    @staticmethod
    def _part(fn: Callable):
        try:
            return to_json(fn()), None
        except CommandError as e:
            return None, e.message
        except Exception as e:  # pragma: no cover - defensive
            log.exception("status part failed")
            return None, str(e)

    def _suggest_route(self, eth) -> Optional[dict]:
        net = eth_network(eth) if eth else None
        if net is None:
            return None
        return {"network": str(net.network_address), "prefix": net.prefixlen, "route": str(net),
                "netmask": str(net.netmask), "source": eth.iface}

    # ------------------------------------------------------------- GET

    def status(self, _body=None, token=None):
        eth, eth_err = self._part(lambda: self._cached("eth", 1.5, self.b.eth_status))
        wifi, wifi_err = self._part(lambda: self._cached("wifi", 1.5, self.b.wifi_status))
        ts, ts_err = self._part(lambda: self._cached("ts", 5.0, self.b.ts_status))
        return 200, {
            "ok": True,
            "version": __version__,
            "system": self.b.system(),
            "eth": eth, "eth_error": eth_err,
            "wifi": wifi, "wifi_error": wifi_err,
            "tailscale": ts, "tailscale_error": ts_err,
            "auth": self.auth.status(token),
        }

    def health(self, _body=None, _token=None):
        return 200, {"ok": True, "version": __version__, "demo": bool(getattr(self.b, "demo", False))}

    def eth_get(self, _body=None, _token=None):
        return 200, {"ok": True, "eth": to_json(self.b.eth_status())}

    def wifi_get(self, _body=None, _token=None):
        status = self.b.wifi_status()
        ap = self.b.ap_config()
        if not ap.exists:
            ap.ssid = ap.ssid or "%s-AP" % self.b.system().get("hostname", "netpanel")
            ap.password = ap.password or self._ap_default_password
        return 200, {"ok": True, "wifi": to_json(status), "ap": to_json(ap)}

    def ts_get(self, _body=None, _token=None):
        ts = self.b.ts_status()
        eth = self.b.eth_status()
        return 200, {"ok": True, "tailscale": to_json(ts), "suggested": self._suggest_route(eth)}

    # ------------------------------------------------------------- auth

    def auth_get(self, _body=None, token=None):
        return 200, dict(ok=True, demo=bool(getattr(self.b, "demo", False)), **self.auth.status(token))

    def auth_login(self, body, _token=None):
        try:
            token = self.auth.login(str(body.get("pin", "")))
        except AuthError as e:
            # 403 (not 401): the UI treats 401 as "session expired"
            return 403, {"ok": False, "message": e.message, "retry_after": e.retry_after,
                         "attempts_left": e.attempts_left}
        return 200, {"ok": True, "token": token, "idle_timeout": self.auth.idle_timeout}

    def auth_logout(self, _body, token=None):
        self.auth.logout(token)
        return 200, {"ok": True}

    def auth_ping(self, _body, _token=None):
        return 200, {"ok": True, "idle_timeout": self.auth.idle_timeout}

    # ------------------------------------------------------------- POST

    def eth_post(self, body, _token=None):
        cfg = validate_ipv4_config(IPv4Config.from_dict(body))
        msg = self._write(self.b.apply_eth, cfg)
        return 200, {"ok": True, "message": msg}

    def wifi_scan(self, _body, _token=None):
        nets = self.b.wifi_scan()
        return 200, {"ok": True, "networks": to_json(nets)}

    def wifi_radio(self, body, _token=None):
        msg = self._write(self.b.wifi_set_radio, bool(body.get("on")))
        return 200, {"ok": True, "message": msg}

    def wifi_ap(self, body, _token=None):
        eth = self.b.eth_status()
        avoid = [n for n in [eth_network(eth)] if n is not None]
        regdomain = None
        try:
            regdomain = self.b.wifi_status().regdomain or "00"
        except CommandError:
            pass
        cfg = validate_ap(APConfig.from_dict(body), avoid_networks=avoid, regdomain=regdomain)
        msg = self._write(self.b.start_ap, cfg)
        return 200, {"ok": True, "message": msg}

    def wifi_ap_stop(self, _body, _token=None):
        msg = self._write(self.b.stop_ap)
        return 200, {"ok": True, "message": msg}

    def wifi_station(self, body, _token=None):
        req = validate_station(StationRequest.from_dict(body))
        msg = self._write(self.b.connect_station, req)
        return 200, {"ok": True, "message": msg}

    def ts_routes(self, body, _token=None):
        if body.get("clear"):
            routes = []
        elif "routes" in body:
            routes = validate_routes(body.get("routes") or [])
        else:
            route, _changed = canonical_route(str(body.get("network", "")), body.get("prefix", ""))
            routes = [route]
        result = self._write(self.b.ts_set_routes, routes)
        return 200, dict(ok=True, routes=routes, **result)

    # ------------------------------------------------------------- LAN scan

    def scan_get(self, _body=None, _token=None):
        nets, err = [], None
        try:
            nets = self.b.scan.local_networks()
        except CommandError as e:
            err = e.message
        eth = None
        try:
            eth = self._cached("eth", 1.5, self.b.eth_status)
        except CommandError:
            pass
        suggested = suggest_range(eth_network(eth) if eth else None, eth.iface if eth else "eth0", nets)
        return 200, {"ok": True, "scan": self.b.scan.snapshot(), "networks": [n.to_json() for n in nets],
                     "suggested": suggested, "error": err}

    def scan_status(self, _body=None, _token=None):
        """Lightweight: polled by the UI while a scan runs."""
        return 200, {"ok": True, "scan": self.b.scan.snapshot()}

    def scan_start(self, body, _token=None):
        net, local = validate_scan_range(body.get("network", ""), body.get("prefix", ""), self.b.scan.local_networks())
        try:
            snap = self.b.scan.start(net, local)
        except ScanBusy:
            return 409, {"ok": False, "message": "A scan is already running - stop it first."}
        return 200, {"ok": True, "scan": snap}

    def scan_stop(self, _body, _token=None):
        return 200, {"ok": True, "scan": self.b.scan.stop()}


ROUTES_GET = {
    "/api/health": "health",
    "/api/status": "status",
    "/api/auth": "auth_get",
    "/api/eth": "eth_get",
    "/api/wifi": "wifi_get",
    "/api/tailscale": "ts_get",
    "/api/scan": "scan_get",
    "/api/scan/status": "scan_status",
}
ROUTES_POST = {
    "/api/eth": "eth_post",
    "/api/wifi/scan": "wifi_scan",
    "/api/wifi/radio": "wifi_radio",
    "/api/wifi/ap": "wifi_ap",
    "/api/wifi/ap/stop": "wifi_ap_stop",
    "/api/wifi/station": "wifi_station",
    "/api/tailscale/routes": "ts_routes",
    "/api/scan/start": "scan_start",
    "/api/scan/stop": "scan_stop",
    "/api/auth/login": "auth_login",
    "/api/auth/logout": "auth_logout",
    "/api/auth/ping": "auth_ping",
}
# Reachable without login: the overview screen and the login itself.
PUBLIC = {"health", "status", "auth_get", "auth_login", "auth_logout"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NetPanel/" + __version__
    protocol_version = "HTTP/1.1"

    # set by make_server()
    api: Api
    web_dir: Path
    allowed_hosts: Optional[set]

    def log_message(self, fmt, *args):  # route http.server logging into `logging`
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- plumbing

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if self.allowed_hosts is None:
            return True
        return self.headers.get("Host", "") in self.allowed_hosts

    def _dispatch(self, name: str, body: dict):
        token = self.headers.get("X-NetPanel-Token") or None
        if name not in PUBLIC and not self.api.auth.check(token):
            return self._json(401, {"ok": False, "auth_required": True, "message": "Login required."})
        try:
            code, payload = getattr(self.api, name)(body, token)
            self._json(code, payload)
        except ValidationError as e:
            self._json(400, {"ok": False, "message": e.message, "errors": e.errors})
        except Busy:
            self._json(409, {"ok": False, "message": "Another change is still in progress. Please wait."})
        except CommandError as e:
            self._json(502, {"ok": False, "message": e.message, "command": e.cmd})
        except Exception as e:
            log.exception("handler %s failed", name)
            self._json(500, {"ok": False, "message": "Internal error: %s" % e})

    # ------------------------------------------------------------- verbs

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"ok": False, "message": "Forbidden host"})
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            name = ROUTES_GET.get(path)
            if not name:
                return self._json(404, {"ok": False, "message": "Unknown endpoint"})
            return self._dispatch(name, {})
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        # Until the body has been read, any early answer must close the
        # connection - otherwise the unread body would be parsed as the next request.
        self.close_connection = True
        if not self._host_ok():
            return self._json(403, {"ok": False, "message": "Forbidden host"})
        path = urlsplit(self.path).path
        name = ROUTES_POST.get(path)
        if not name:
            return self._json(404, {"ok": False, "message": "Unknown endpoint"})
        if self.headers.get("X-NetPanel") != "1" or "application/json" not in self.headers.get("Content-Type", ""):
            return self._json(403, {"ok": False, "message": "Missing X-NetPanel header"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._json(413, {"ok": False, "message": "Body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        self.close_connection = False
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"ok": False, "message": "Invalid JSON"})
        return self._dispatch(name, body)

    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        root = self.web_dir.resolve()
        target = (root / path.lstrip("/")).resolve()
        if root not in target.parents and target != root or not target.is_file():
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def make_server(backend, host: str = "127.0.0.1", port: int = 8088, web_dir: Optional[Path] = None,
                auth: Optional[AuthManager] = None):
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("font/otf", ".otf")
    mimetypes.add_type("image/svg+xml", ".svg")
    web = Path(web_dir) if web_dir else Path(__file__).resolve().parent.parent / "web"

    loopback = False
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"

    allowed = None
    if loopback:
        allowed = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
    else:
        log.warning("Listening on %s - the API is reachable from the network!", host)

    handler = type("NetPanelHandler", (Handler,), {
        "api": Api(backend, auth or AuthManager(None)), "web_dir": web, "allowed_hosts": allowed,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
