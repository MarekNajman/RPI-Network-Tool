#!/bin/bash
# Started by cage (see netpanel-kiosk.service). Waits for the NetPanel API and
# then runs Chromium in kiosk mode. When Chromium exits, cage exits and systemd
# restarts the whole kiosk.
set -u

PORT="${NETPANEL_PORT:-8088}"
URL="http://127.0.0.1:${PORT}/"

BROWSER="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$BROWSER" ]; then
    echo "kiosk.sh: Chromium not found (apt install chromium-browser)" >&2
    sleep 10
    exit 1
fi

# Wait up to 60 s for the API (it starts in parallel at boot).
for _ in $(seq 1 120); do
    if python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1] + 'api/health', timeout=1)" "$URL" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

# Fresh throw-away profile on every start: no "restore pages" bubbles, no stale cache.
PROFILE="${XDG_RUNTIME_DIR:-/tmp}/netpanel-chromium"
rm -rf "$PROFILE"
mkdir -p "$PROFILE"

exec "$BROWSER" \
    --kiosk "$URL" \
    --ozone-platform=wayland \
    --user-data-dir="$PROFILE" \
    --force-device-scale-factor=1 \
    --no-first-run \
    --no-default-browser-check \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-translate \
    --disable-features=Translate,TranslateUI,OverscrollHistoryNavigation,TouchpadOverscrollHistoryNavigation,MediaRouter \
    --overscroll-history-navigation=0 \
    --disable-pinch \
    --enable-features=OverlayScrollbar \
    --password-store=basic \
    --disable-sync \
    --disable-component-update \
    --check-for-update-interval=31536000 \
    --autoplay-policy=user-gesture-required
