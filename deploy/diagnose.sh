#!/bin/bash
# NetPanel diagnostics - collects everything needed to debug a black kiosk screen.
#
#   sudo ./deploy/diagnose.sh                 report -> /tmp/netpanel-diag.txt
#   sudo ./deploy/diagnose.sh --kiosk-debug   + restart the kiosk once with debug logging (~20 s)
#
# The report contains hostnames, IP addresses and package versions, but no
# passwords, PINs or Wi-Fi keys. Check it before you share it.
set -u
OUT=/tmp/netpanel-diag.txt
KIOSK_DEBUG=0
[ "${1:-}" = "--kiosk-debug" ] && KIOSK_DEBUG=1
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }

CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

sec() { printf '\n========== %s ==========\n' "$*"; }
run() { printf '$ %s\n' "$*"; bash -c "$*" 2>&1 | head -n "${LINES_MAX:-80}"; }

exec > >(tee "$OUT") 2>&1

sec "SYSTEM"
run "date; hostname; uptime"
run "cat /proc/device-tree/model; echo"
run "grep -E 'PRETTY_NAME|VERSION_CODENAME' /etc/os-release"
run "uname -a"
run "dpkg --print-architecture"
run "vcgencmd version 2>/dev/null"

sec "BOOT CONFIG ($CFG) - active lines (comments/blank removed)"
run "grep -nvE '^[[:space:]]*(#|$)' '$CFG'"
sec "BOOT CONFIG - full file"
LINES_MAX=300 run "cat -n '$CFG'"
sec "CMDLINE ($CMDLINE)"
run "cat '$CMDLINE'"

sec "GRAPHICS STACK (KMS vs legacy framebuffer)"
run "ls -l /sys/class/drm/"
run "ls -l /dev/dri/ 2>&1"
run "cat /proc/fb"
run "ls -l /dev/fb* 2>&1"
run "for f in /sys/class/graphics/fb*/name /sys/class/graphics/fb*/virtual_size; do echo \"\$f: \$(cat \$f)\"; done"
run "for c in /sys/class/drm/card*-*; do [ -e \"\$c\" ] && echo \"\$(basename \$c): \$(cat \$c/status) \$(head -1 \$c/modes 2>/dev/null)\"; done"
run "lsmod | grep -E '^(vc4|v3d|drm|drm_kms_helper|panel|edt_ft5x06|rpi_ft5406|tc358762|rpi_panel|bcm2708_fb)' || echo '(none of the display modules loaded)'"
run "cat /proc/device-tree/soc/gpu/compatible 2>/dev/null | tr '\0' ' '; echo; cat /proc/device-tree/soc/gpu/status 2>/dev/null; echo"
run "grep -rsE 'vc4|v3d|drm' /etc/modprobe.d/ /lib/modprobe.d/ || echo '(no vc4/drm blacklist)'"
LINES_MAX=60 run "dmesg | grep -iE 'vc4|v3d|drm|dsi|panel|ft5|fb0|framebuffer|tc358|hdmi' | tail -n 60"
run "vcgencmd get_config int 2>/dev/null | grep -iE 'display|lcd|hdmi|framebuffer|ignore' "
run "kmsprint 2>/dev/null | head -20 || echo '(kmsprint not available)'"

sec "TOUCH / INPUT"
run "grep -E '^N: Name|^H: Handlers' /proc/bus/input/devices"
run "id netpanel 2>&1"

sec "PRIVILEGES (polkit / sudo) - same checks as deploy/check-permissions.sh"
LINES_MAX=60 run "bash '$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-permissions.sh'"

sec "BOOT TARGET / DESKTOP / VNC"
run "systemctl get-default"
run "systemctl is-enabled display-manager.service lightdm.service getty@tty1.service 2>&1"
run "ls -l /etc/systemd/system/display-manager.service 2>&1"
run "ls /usr/bin/labwc /usr/bin/wayfire /usr/bin/Xorg /usr/bin/xinit /usr/bin/cage 2>&1"
run "grep -sE '^(autologin-user|user-session|greeter-session)' /etc/lightdm/lightdm.conf"
run "systemctl list-units --all --no-pager '*vnc*'"
run "systemctl list-unit-files --no-pager '*vnc*'"
LINES_MAX=30 run "journalctl -b --no-pager -o short-monotonic | grep -i vnc | tail -n 20"
run "ss -ltnp | grep -E ':59[0-9][0-9]|:8088' || true"

sec "NETPANEL SERVICES"
run "systemctl --no-pager --full status netpanel.service netpanel-kiosk.service netpanel-kiosk-x11.service 2>&1 | head -n 60"
LINES_MAX=60 run "journalctl -b --no-pager -u netpanel.service | tail -n 40"
LINES_MAX=80 run "journalctl -b --no-pager -u netpanel-kiosk.service -u netpanel-kiosk-x11.service | tail -n 80"
run "loginctl list-sessions --no-pager"
run "for s in \$(loginctl list-sessions --no-legend | awk '\$3==\"netpanel\"{print \$1}'); do loginctl show-session \$s -p Type -p Class -p Seat -p VTNr -p TTY -p Active -p State; done"
run "curl -s -m 3 http://127.0.0.1:8088/api/health || echo 'API not reachable'"
run "cage -v 2>&1; (command -v chromium-browser || command -v chromium) && (chromium-browser --version 2>/dev/null || chromium --version 2>/dev/null)"
run "dpkg -l 'cage' 'chromium*' 'xserver-xorg-core' 'xserver-xorg-legacy' 'xinit' 2>/dev/null | grep '^ii' | awk '{print \$2, \$3}'"
run "cat /etc/X11/Xwrapper.config 2>/dev/null; ls /etc/X11/xorg.conf.d/ 2>/dev/null"
run "tail -n 30 /var/log/Xorg.0.log 2>/dev/null || tail -n 30 /var/lib/netpanel/.local/share/xorg/Xorg.0.log 2>/dev/null || echo '(no Xorg log)'"

if [ "$KIOSK_DEBUG" = 1 ] && systemctl is-enabled --quiet netpanel-kiosk.service 2>/dev/null; then
    sec "KIOSK DEBUG RUN (cage -D, 20 s)"
    mkdir -p /etc/systemd/system/netpanel-kiosk.service.d
    cat > /etc/systemd/system/netpanel-kiosk.service.d/zz-debug.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/cage -d -D -- /opt/netpanel/deploy/kiosk.sh
Environment=WLR_LOG=debug
EOF
    systemctl daemon-reload
    since="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl restart netpanel-kiosk.service
    sleep 20
    LINES_MAX=150 run "journalctl --no-pager -u netpanel-kiosk.service --since '$since' | grep -vE 'Scheduled restart|Stopped|Starting|Started' | tail -n 150"
    rm -f /etc/systemd/system/netpanel-kiosk.service.d/zz-debug.conf
    rmdir /etc/systemd/system/netpanel-kiosk.service.d 2>/dev/null
    systemctl daemon-reload
    systemctl restart netpanel-kiosk.service
fi

sec "VERDICT (automatic hints)"
if ls /sys/class/drm/card*-* >/dev/null 2>&1; then
    echo "[OK]   KMS display driver is active:"
    for c in /sys/class/drm/card*-*; do echo "       $(basename "$c"): $(cat "$c/status")"; done
    ls /sys/class/drm/ | grep -q DSI || echo "[WARN] KMS is active but no DSI connector -> DSI overlay missing (dtoverlay=vc4-kms-dsi-7inch)"
    if [ "$(ls /sys/class/drm/ | grep -c -- '-')" -gt 1 ]; then
        echo "[HINT] Several outputs. If the kiosk stays black, pin the DSI card in /etc/default/netpanel:"
        echo "       WLR_DRM_DEVICES=/dev/dri/$(ls /sys/class/drm | grep DSI | head -1 | cut -d- -f1)"
    fi
else
    echo "[FAIL] NO KMS display driver (/sys/class/drm has no cardN). The display runs on the legacy"
    echo "       firmware framebuffer: $(tr '\n' ' ' 2>/dev/null < /proc/fb)"
    echo "       -> cage/Wayland CANNOT run here. Choose one fix (README, section 'Displej: KMS vs. legacy'):"
    echo "          A) enable KMS:          sudo /opt/netpanel/deploy/display-kms.py   (dry run, then --apply)"
    echo "          B) X11 kiosk on legacy: sudo ./deploy/install.sh --kiosk-mode x11"
fi
if systemctl list-units --all --no-legend '*vnc*' 2>/dev/null | grep -q .; then
    echo "[INFO] A VNC service exists. With the desktop disabled it cannot attach to a desktop and logs errors."
    echo "       Not needed:  sudo systemctl disable --now <vnc unit>    Needed: see README (VNC)."
fi
curl -s -m 3 http://127.0.0.1:8088/api/health >/dev/null && echo "[OK]   NetPanel API is running" || echo "[FAIL] NetPanel API not reachable: journalctl -u netpanel -e"

echo
echo "Report saved to $OUT"
