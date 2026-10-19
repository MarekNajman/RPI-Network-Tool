#!/bin/bash
# X11 client started by xinit (netpanel-kiosk-x11.service).
# Disables screen blanking and hands over to kiosk.sh with the X11 settings.
set -u

xset s off 2>/dev/null
xset s noblank 2>/dev/null
xset -dpms 2>/dev/null

# Screen size for the Chromium window (there is no window manager to maximise it).
SIZE="$(xrandr 2>/dev/null | sed -n 's/.*current \([0-9]*\) x \([0-9]*\).*/\1,\2/p' | head -n 1)"
if [ -z "$SIZE" ] && [ -r /sys/class/graphics/fb0/virtual_size ]; then
    SIZE="$(cat /sys/class/graphics/fb0/virtual_size)"
fi

export NETPANEL_KIOSK_PLATFORM=x11
export NETPANEL_WINDOW_SIZE="${NETPANEL_WINDOW_SIZE:-${SIZE:-800,480}}"
exec /opt/netpanel/deploy/kiosk.sh
