"""Entry point:  python3 -m netpanel [--demo] [--port 8088] [--eth eth0] [--wlan wlan0]"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from . import __version__
from .server import make_server


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="netpanel", description="NetPanel network configurator (API + web UI)")
    p.add_argument("--host", default=os.environ.get("NETPANEL_HOST", "127.0.0.1"),
                   help="listen address (default 127.0.0.1 - keep it local!)")
    p.add_argument("--port", type=int, default=int(os.environ.get("NETPANEL_PORT", "8088")))
    p.add_argument("--eth", default=os.environ.get("NETPANEL_ETH", "eth0"), help="Ethernet interface")
    p.add_argument("--wlan", default=os.environ.get("NETPANEL_WLAN", "wlan0"), help="Wi-Fi interface")
    p.add_argument("--web-dir", default=os.environ.get("NETPANEL_WEB_DIR"), help="override static files dir")
    p.add_argument("--demo", action="store_true", default=os.environ.get("NETPANEL_DEMO") == "1",
                   help="simulate everything (for development on a PC)")
    p.add_argument("--demo-delay", type=float, default=1.2, help="seconds per simulated operation")
    p.add_argument("--log-level", default=os.environ.get("NETPANEL_LOG", "INFO"))
    p.add_argument("--version", action="version", version="netpanel " + __version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("netpanel")

    if args.demo:
        from .backend.demo import DemoBackend

        backend = DemoBackend(eth=args.eth, wlan=args.wlan, delay=args.demo_delay)
        log.warning("DEMO MODE - no system changes will be made")
    else:
        from .backend import Backend

        backend = Backend(eth=args.eth, wlan=args.wlan)

    httpd = make_server(backend, host=args.host, port=args.port, web_dir=args.web_dir)
    log.info("NetPanel %s listening on http://%s:%d (eth=%s, wlan=%s)",
             __version__, args.host, args.port, args.eth, args.wlan)

    def _stop(signum, _frame):
        log.info("signal %d - shutting down", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
