#!/bin/bash
# Remove NetPanel services and privileges.
#   sudo ./deploy/uninstall.sh            keep /opt/netpanel, user and settings
#   sudo ./deploy/uninstall.sh --purge    remove everything NetPanel installed
#
# NetworkManager profiles (netpanel-eth / netpanel-ap / netpanel-sta) are KEPT -
# they are your current network configuration. Delete them with nmcli if needed.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

systemctl disable --now netpanel-kiosk.service netpanel-kiosk-x11.service netpanel.service 2>/dev/null || true
rm -f /etc/systemd/system/netpanel.service /etc/systemd/system/netpanel-kiosk.service /etc/systemd/system/netpanel-kiosk-x11.service
rm -f /etc/X11/xorg.conf.d/99-netpanel-fbdev.conf
[ -f /etc/X11/Xwrapper.config.netpanel-bak ] && mv /etc/X11/Xwrapper.config.netpanel-bak /etc/X11/Xwrapper.config
rm -f /etc/pam.d/netpanel /etc/sudoers.d/netpanel /usr/local/sbin/netpanel-ts-routes /usr/local/sbin/netpanel-passwd
rm -f /etc/polkit-1/rules.d/10-netpanel.rules /etc/polkit-1/rules.d/50-netpanel.rules /etc/polkit-1/localauthority/50-local.d/50-netpanel.pkla
systemctl daemon-reload
systemctl enable getty@tty1.service >/dev/null 2>&1 || true

if [ "$PURGE" = 1 ]; then
    rm -rf /opt/netpanel /etc/netpanel /etc/default/netpanel /etc/sysctl.d/99-netpanel-ipforward.conf
    if id netpanel >/dev/null 2>&1; then
        loginctl terminate-user netpanel 2>/dev/null || true
        systemctl stop "user@$(id -u netpanel).service" 2>/dev/null || true
        sleep 1
        userdel -r netpanel || echo "WARNING: could not remove user 'netpanel' (still logged in?) - run: sudo userdel -r netpanel" >&2
    fi
fi

echo "NetPanel removed."
if systemctl list-unit-files lightdm.service >/dev/null 2>&1 && [ -e /usr/sbin/lightdm ]; then
    echo "Desktop back on boot:  sudo systemctl set-default graphical.target && sudo reboot"
fi
