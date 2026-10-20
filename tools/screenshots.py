#!/usr/bin/env python3
"""Render every NetPanel screen at 800x480 using the demo backend.

Requires Playwright for Python with Chromium (development machine only):
    python3 tools/screenshots.py [out_dir]

Also acts as a UI smoke test: it fails on JavaScript errors and walks
through the main flows (static IP, AP start, station connect, advertise).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    port = free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "netpanel", "--demo", "--demo-delay", "0.4", "--port", str(port), "--log-level", "WARNING"],
        cwd=ROOT,
    )
    errors = []
    try:
        time.sleep(1.0)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 480}, has_touch=True, device_scale_factor=1)
            page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
            page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append("console: %s" % m.text))
            page.goto("http://127.0.0.1:%d/" % port)
            page.wait_for_selector(".tile .pill.ok")
            page.wait_for_timeout(600)

            def shot(name):
                page.wait_for_timeout(350)
                page.screenshot(path=str(OUT / ("%s.png" % name)))
                print("saved", name)

            def tap(selector):
                page.locator(selector).first.tap()

            shot("01-home")

            def pin(digits):
                for d in digits:
                    page.locator(".keypad.pin .key", has_text=d).first.tap()
                page.locator(".keypad.pin .key.done").tap()

            # ---------------- Login (any page except home needs the PIN)
            tap(".tile.eth")
            page.wait_for_selector(".kb.login")
            page.wait_for_timeout(300)
            shot("21-login")
            pin("111111")
            page.wait_for_selector(".login-msg.bad")
            shot("22-login-wrong-pin")
            pin("123456")

            # ---------------- Ethernet
            page.wait_for_selector("#page-eth.active")
            page.wait_for_selector(".toast", state="detached", timeout=8000)
            shot("02-ethernet-dhcp")
            page.locator("#page-eth .seg button", has_text="Static IP").tap()
            shot("03-ethernet-static")
            page.locator("#page-eth .field", has_text="IP address").locator(".input").tap()
            page.wait_for_selector(".kb.numeric")
            for ch in "192.168.10.50":
                page.locator(".keypad .key", has_text=ch).first.tap() if ch != "." else page.locator(".keypad .key", has_text=".").tap()
            shot("04-keypad-ip")
            page.locator(".keypad .key.done").tap()
            page.locator("#page-eth .btn.primary", has_text="Apply").tap()
            page.wait_for_selector(".modal")
            shot("05-confirm-static")
            page.locator(".modal .btn.primary").tap()
            page.wait_for_selector(".toast")
            shot("06-static-applied")
            page.locator("#page-eth .btn-back").tap()

            # ---------------- Wi-Fi station
            tap(".tile.wifi")
            page.wait_for_selector("#page-wifi .net")
            shot("07-wifi-station")
            page.locator("#page-wifi .net", has_text="Workshop-2G").tap()
            page.wait_for_selector("#page-station.active")
            shot("08-station-connect")
            page.locator("#page-station .field", has_text="Password").locator(".input").tap()
            page.wait_for_selector(".kb:not(.numeric)")
            for ch in "Secret":
                if ch.isupper():
                    page.locator(".kb .key.w15").first.tap()
                page.locator(".kb-row .key", has_text=ch.lower() if ch.islower() else ch).first.tap()
            shot("09-keyboard-text")
            page.locator(".kb .key.fn", has_text="?123").tap()
            shot("10-keyboard-symbols")
            for ch in "12345678":
                page.locator(".kb-row .key", has_text=ch).first.tap()
            page.locator(".kb-top .btn.primary").tap()
            page.locator("#page-station .btn.teal", has_text="Connect").tap()
            page.wait_for_selector("#page-wifi.active")
            page.wait_for_timeout(600)

            # ---------------- Wi-Fi AP
            page.locator("#page-wifi .seg button", has_text="Access Point").tap()
            shot("11-wifi-ap-form")
            page.locator("#page-wifi .field", has_text="Channel").locator(".input").tap()
            page.wait_for_selector(".choices")
            shot("12-channel-chooser")
            page.locator(".choices .btn", has_text="11").tap()
            page.locator("#page-wifi .btn.teal", has_text="Start access point").tap()
            page.wait_for_selector(".modal")
            shot("13-confirm-ap")
            page.locator(".modal .btn.teal").tap()
            page.wait_for_selector(".toast")
            page.wait_for_timeout(800)
            shot("14-ap-running")
            page.locator("#page-wifi .seg button", has_text="Station").tap()
            shot("15-station-while-ap")
            page.locator("#page-wifi .btn-back").tap()

            # ---------------- Tailscale
            tap(".adv-btn")
            page.wait_for_selector("#page-ts.active")
            page.wait_for_timeout(500)
            shot("16-tailscale")
            page.locator("#page-ts .btn.violet").tap()
            page.wait_for_selector(".modal")
            shot("17-confirm-advertise")
            page.locator(".modal .btn.violet").tap()
            page.wait_for_selector(".toast")
            page.wait_for_timeout(500)
            shot("18-tailscale-advertised")
            page.locator("#page-ts .btn-back").tap()
            page.evaluate("window.netpanel.refresh()")
            page.wait_for_selector(".toast", state="detached", timeout=8000)
            shot("19-home-after")

            # ---------------- inactivity: warning 30 s before, then automatic logout
            page.evaluate("window.netpanel.session.lastActivity = Date.now() - (window.netpanel.session.idleMs - 25000)")
            page.wait_for_selector(".idle-banner", timeout=3000)
            shot("24-idle-warning")
            page.evaluate("window.netpanel.session.lastActivity = Date.now() - window.netpanel.session.idleMs - 1000")
            page.wait_for_selector(".btn-session:not(.on)", timeout=3000)
            page.wait_for_selector(".toast.err")
            shot("25-auto-logout")
            # log in again from the topbar button
            tap(".btn-session")
            page.wait_for_selector(".kb.login")
            pin("123456")
            page.wait_for_selector(".btn-session.on")
            page.wait_for_selector(".toast", state="detached", timeout=8000)
            shot("23-home-logged-in")

            # ---------------- LAN scan
            tap(".scan-tile")
            page.wait_for_selector("#page-scan.active")
            page.wait_for_timeout(500)
            shot("26-scan-idle")
            tap("#page-scan .scan-go")
            page.locator("#page-scan .scan-list .scan-row").nth(4).wait_for(timeout=8000)
            shot("27-scan-running")
            page.wait_for_selector("#page-scan .scan-go:not(.stop)", timeout=15000)
            page.wait_for_timeout(300)
            shot("28-scan-results")
            page.locator("#page-scan .scan-list").evaluate("el => el.scrollTop = el.scrollHeight")
            shot("29-scan-results-scrolled")
            page.locator("#page-scan .scan-row", has_text="nas01").tap()
            page.wait_for_selector(".modal")
            shot("30-scan-detail")
            page.locator(".modal .btn.primary").tap()
            page.locator("#page-scan .scan-row", has_text="Galaxy-Tab").tap()
            page.wait_for_selector(".modal")
            shot("31-scan-detail-private-mac")
            page.locator(".modal .btn.primary").tap()
            # too large: /16
            page.locator("#page-scan .f-pfx .input").tap()
            page.wait_for_selector(".kb.numeric")
            for ch in "16":
                page.locator(".keypad .key", has_text=ch).first.tap()
            page.locator(".keypad .key.done").tap()
            shot("32-scan-prefix-too-large")
            page.locator(".kb .btn", has_text="Cancel").tap()
            # not a directly connected network
            page.locator("#page-scan .f-net .input").tap()
            page.wait_for_selector(".kb.numeric")
            for ch in "172.16.5.0":
                page.locator(".keypad .key", has_text=ch).first.tap() if ch != "." else page.locator(".keypad .key", has_text=".").tap()
            page.locator(".keypad .key.done").tap()
            tap("#page-scan .scan-go")
            page.wait_for_selector("#page-scan .empty.bad")
            shot("33-scan-not-local")
            page.locator("#page-scan .empty .chips .btn").first.tap()
            page.wait_for_timeout(200)
            page.locator("#page-scan .btn-back").tap()
            page.wait_for_selector("#page-home.active")

            # ---------------- error path: wrong Wi-Fi password
            page.evaluate("window.netpanel.go('wifi')")
            page.locator("#page-wifi .seg button", has_text="Access Point").tap()
            page.locator("#page-wifi .btn.danger", has_text="Stop AP").tap()
            page.locator(".modal .btn.danger").tap()
            page.wait_for_selector("#page-wifi .net", timeout=8000)
            page.locator("#page-wifi .net", has_text="Office-WiFi").tap()
            page.locator("#page-station .field", has_text="Password").locator(".input").tap()
            page.keyboard.type("wrongpass")
            page.keyboard.press("Enter")
            page.locator("#page-station .btn.teal").tap()
            page.wait_for_selector(".modal.error", timeout=8000)
            shot("20-error-dialog")
            browser.close()
    except Exception:
        try:
            page.screenshot(path=str(OUT / "FAILED.png"))
        except Exception:
            pass
        raise
    finally:
        srv.terminate()
        srv.wait(5)
    if errors:
        print("\nJS ERRORS:\n" + "\n".join(errors))
        return 1
    print("OK - no JS errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
