#!/bin/bash
# Runs Chromium in kiosk mode on the NetPanel UI.
#   Wayland: started by cage   (netpanel-kiosk.service)
#   X11:     started by xinit via kiosk-x11.sh (netpanel-kiosk-x11.service)
# When Chromium exits, cage/xinit exit and systemd restarts the whole kiosk.
set -u

PORT="${NETPANEL_PORT:-8088}"
URL="http://127.0.0.1:${PORT}/"
PLATFORM="${NETPANEL_KIOSK_PLATFORM:-wayland}"

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

PLATFORM_ARGS=()
if [ "$PLATFORM" = "x11" ]; then
    # No window manager: place and size the window ourselves.
    PLATFORM_ARGS+=(--ozone-platform=x11 --window-position=0,0 "--window-size=${NETPANEL_WINDOW_SIZE:-800,480}")
    # Legacy framebuffer = no GPU device; avoid the GPU process crash loop.
    ls /dev/dri/card* >/dev/null 2>&1 || PLATFORM_ARGS+=(--disable-gpu)
else
    PLATFORM_ARGS+=(--ozone-platform=wayland)
fi
echo "kiosk.sh: platform=$PLATFORM browser=$BROWSER ${PLATFORM_ARGS[*]}" >&2

exec "$BROWSER" \
    --kiosk "$URL" \
    "${PLATFORM_ARGS[@]}" \
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
