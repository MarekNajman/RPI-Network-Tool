#!/bin/bash
# Touch screen diagnostics for the NetPanel kiosk (Waveshare 5inch DSI on a Pi 4).
#
#   sudo ./deploy/check-touch.sh          report only
#   sudo ./deploy/check-touch.sh --test   + 15 s live test: touch the screen while it runs
#
# Changes nothing. Shows which layer loses the touch:
#   config.txt overlays -> kernel driver -> input device -> events -> kiosk session
set -u
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 ${1:-}" >&2; exit 1; }
TEST=0
[ "${1:-}" = "--test" ] && TEST=1

CFG="${NETPANEL_BOOTCFG:-/boot/firmware/config.txt}"
[ -f "$CFG" ] || CFG=/boot/config.txt

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
info() { printf '       %s\n' "$*"; }

# --------------------------------------------------------------------------- 1
echo "== 1. Display / touch related lines in $CFG  ([section] line)"
ACTIVE="$(awk '
    /^[[:space:]]*(#|$)/ { next }
    /^[[:space:]]*\[/    { sec = $0; gsub(/[[:space:]]/, "", sec); next }
    /dtoverlay|display_|touch|lcd|dtparam=(spi|i2c)/ { printf "%s %s\n", (sec == "" ? "[all]" : sec), $0 }
' "$CFG")"
if [ -n "$ACTIVE" ]; then printf '%s\n' "$ACTIVE" | sed 's/^/       /'; else info "(none)"; fi

has() { printf '%s\n' "$ACTIVE" | grep -E "^\[(all|pi4)\]" | grep -qE "$1"; }
CONFLICT=0
if has 'dtoverlay=rpi-ft5406'; then
    warn "dtoverlay=rpi-ft5406 - legacy (firmware) touch driver for DSI panels."
    info "Under KMS the touch comes from vc4-kms-dsi-7inch (kernel driver edt-ft5x06); both on the"
    info "same controller are a likely conflict -> comment rpi-ft5406 out (see README, section Dotyk)."
    CONFLICT=1
fi
if has 'disable_touchscreen=1'; then
    warn "disable_touchscreen=1 - touch explicitly disabled"; CONFLICT=1
fi
if has 'vc4-kms-dsi-7inch.*disable_touch'; then
    warn "vc4-kms-dsi-7inch has the disable_touch parameter"; CONFLICT=1
fi
if has 'dtparam=i2c_vc=on'; then
    warn "dtparam=i2c_vc=on - exposes the I2C bus that the DSI display/touch uses; can disturb the touch controller"
fi
if printf '%s\n' "$ACTIVE" | grep -qE 'pitft|stmpe|ads7846|rpi-display|waveshare3|tft35|piscreen|hy28'; then
    warn "Overlay for a DIFFERENT (SPI) display/touch is active - leftover from another screen."
    info "It is the source of boot messages like 'stmpe-spi spi0.1: unknown chip id: 0x0'"
    info "(that chip is not connected). Not the cause by itself, but remove it - it does not belong here."
fi
if has 'display_auto_detect=1' && has 'vc4-kms-dsi-7inch'; then
    info "display_auto_detect=1 and an explicit vc4-kms-dsi-7inch are both set (overlay may load twice)."
    info "Usually harmless. Only if touch stays dead after everything else: try display_auto_detect=0."
fi
if ! has 'vc4-kms-dsi'; then
    warn "no active vc4-kms-dsi-* overlay for the Pi 4 - the KMS touch driver will not be loaded"
fi

# --------------------------------------------------------------------------- 2
echo "== 2. Kernel modules"
MODS="$(lsmod | awk '{print $1}' | grep -E '^(edt_ft5x06|rpi_ft5406|goodix|goodix_ts|stmpe_ts|stmpe|ads7846|panel_raspberrypi_touchscreen|rpi_panel_attiny_regulator|tc358762|regulator_rpi_panel_attiny)$' | tr '\n' ' ')"
info "${MODS:-(no touch/panel modules loaded)}"

# --------------------------------------------------------------------------- 3
echo "== 3. Kernel log (touch / panel / I2C)"
dmesg | grep -iE 'ft5|edt|goodix|touch|stmpe|ads7846|attiny|tc358762|rpi.panel|i2c.*(timeout|error|fail)' | tail -n 25 | sed 's/^/       /'

echo "== 3b. Same from the PREVIOUS boot (compare: did touch work then?)"
PREV="$(journalctl -k -b -1 --no-pager 2>/dev/null)"
if [ -n "$PREV" ]; then
    printf '%s\n' "$PREV" | grep -iE 'ft5|edt|goodix|touch|stmpe|attiny|tc358762|rpi.panel' | tail -n 15 | sed 's/^/       /'
    info "(boot list: journalctl --list-boots ; older boots: journalctl -k -b -2 ...)"
else
    info "(no previous boot stored - the journal is not persistent; enable: sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald)"
fi

# --------------------------------------------------------------------------- 4
echo "== 4. Touch input devices"
TOUCH_DEVS=()
for d in /dev/input/event*; do
    [ -e "$d" ] || continue
    props="$(udevadm info -q property -n "$d" 2>/dev/null)"
    name="$(cat "/sys/class/input/$(basename "$d")/device/name" 2>/dev/null)"
    if printf '%s\n' "$props" | grep -q '^ID_INPUT_TOUCHSCREEN=1'; then
        TOUCH_DEVS+=("$d")
        ok "$d  \"$name\"  (touchscreen)"
    else
        info "$d  \"$name\""
    fi
done
[ "${#TOUCH_DEVS[@]}" -gt 0 ] || bad "no touchscreen input device - the kernel does not see the touch controller"

# --------------------------------------------------------------------------- 5
echo "== 5. Kiosk session (who receives the touch)"
SEAT="$(loginctl seat-status seat0 --no-pager 2>/dev/null | grep -E 'input:event' | sed 's/^[^a-zA-Z/]*//' | head -n 10)"
[ -n "$SEAT" ] && info "devices on seat0:" && printf '%s\n' "$SEAT" | sed 's/^/         /'
SESS="$(loginctl list-sessions --no-legend 2>/dev/null | awk '$3=="netpanel"{print $1}' | head -n 1)"
if [ -n "$SESS" ]; then
    if [ "$(loginctl show-session "$SESS" -p Active --value 2>/dev/null)" = "yes" ]; then
        ok "kiosk session $SESS of user netpanel is active (gets input from logind)"
    else
        bad "kiosk session $SESS exists but is NOT active -> no input is delivered to it"
        info "is another session in the foreground? loginctl list-sessions"
    fi
else
    warn "no logind session of user netpanel - is the kiosk running? systemctl status netpanel-kiosk"
fi

# --------------------------------------------------------------------------- 6
EVENTS=-1
if [ "$TEST" = 1 ] && [ "${#TOUCH_DEVS[@]}" -gt 0 ]; then
    echo "== 6. Live test: TOUCH THE SCREEN NOW (15 s) ..."
    EVENTS="$(python3 - "${TOUCH_DEVS[@]}" <<'PY'
import os, select, sys, time
fds = []
for path in sys.argv[1:]:
    try:
        fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
    except OSError as e:
        print("cannot open %s: %s" % (path, e), file=sys.stderr)
reads = 0
end = time.time() + 15
while fds and time.time() < end:
    r, _, _ = select.select(fds, [], [], 0.5)
    for fd in r:
        try:
            if os.read(fd, 4096):
                reads += 1
        except BlockingIOError:
            pass
print(reads)
PY
)"
    if [ "${EVENTS:-0}" -gt 0 ]; then ok "touch controller delivers events ($EVENTS reads)"
    else bad "no events in 15 s"; fi
fi

# --------------------------------------------------------------------------- verdict
echo
echo "== VERDICT"
if [ "${#TOUCH_DEVS[@]}" -eq 0 ]; then
    echo "  The kernel does not see the touch controller (layer: config.txt / driver / cable)."
    echo "  1) Fix the WARN lines from section 1 (backup first, see README 'Dotyk')."
    echo "  2) Power off completely for 10 s (not just reboot) - DSI touch controllers sometimes hang."
    echo "  3) Check the flat cable seating on both ends."
elif [ "$EVENTS" = 0 ]; then
    echo "  Touch device exists but sends nothing (layer: controller / I2C)."
    echo "  Most often a conflict with another touch driver (WARN in section 1) or a hung controller:"
    echo "  power off for 10 s, then re-test:  sudo $0 --test"
elif [ "$EVENTS" -gt 0 ]; then
    echo "  Kernel touch works. If the panel still does not react, the kiosk does not get it:"
    echo "  sudo systemctl restart netpanel-kiosk  and  sudo ./deploy/diagnose.sh --kiosk-debug"
    echo "  (look for 'touch' / 'input' devices in the cage log)."
else
    echo "  Run the live test:  sudo $0 --test   and touch the screen during the 15 s."
fi
[ "$CONFLICT" = 1 ] && echo "  NOTE: config.txt contains a likely touch conflict - see the WARN lines above."
exit 0
