#!/bin/bash
# NetPanel installer for Raspberry Pi OS 12 "Bookworm" (Desktop or Lite), Raspberry Pi 4.
#
#   sudo ./deploy/install.sh [options]
#
#   --wifi-country CC   set the Wi-Fi regulatory country (e.g. CZ) via raspi-config
#   --no-kiosk          install only the API service (no cage/Chromium, no tty1 kiosk)
#   --no-ipforward      do not enable net.ipv4.ip_forward (Tailscale subnet routing needs it!)
#   -y, --yes           do not ask questions
#
# Safe to re-run (upgrade = run it again from a newer checkout).
set -euo pipefail

APP_DIR=/opt/netpanel
APP_USER=netpanel
APP_HOME=/var/lib/netpanel
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

KIOSK=1
IPFWD=1
COUNTRY=""
ASSUME_YES=0

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  {
    [ "$ASSUME_YES" = 1 ] && return 0
    read -r -p "$1 [y/N] " a
    [[ "$a" =~ ^[YyAa]$ ]]
}

while [ $# -gt 0 ]; do
    case "$1" in
        --wifi-country) COUNTRY="${2:-}"; shift 2 ;;
        --no-kiosk) KIOSK=0; shift ;;
        --no-ipforward) IPFWD=0; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

# ----------------------------------------------------------------- preflight
[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
[ -f "$SRC_DIR/netpanel/__main__.py" ] || die "run this script from the NetPanel source tree"

. /etc/os-release
if [ "${VERSION_CODENAME:-}" != "bookworm" ]; then
    warn "tested on Raspberry Pi OS bookworm, this is '${VERSION_CODENAME:-unknown}'"
    ask "Continue anyway?" || exit 1
fi

if ! systemctl is-active --quiet NetworkManager; then
    die "NetworkManager is not running. NetPanel configures the network through it.
       Switch with: sudo raspi-config -> Advanced Options -> Network Config -> NetworkManager"
fi

if [ "$KIOSK" = 1 ]; then
    if ! systemctl is-enabled --quiet ssh 2>/dev/null && ! systemctl is-enabled --quiet sshd 2>/dev/null; then
        warn "SSH is NOT enabled. After reboot tty1 shows only the kiosk (no login prompt)."
        warn "Enable SSH first so you can still administer the Pi: sudo systemctl enable --now ssh"
        ask "Continue without SSH?" || exit 1
    fi
    if ! ls /sys/class/drm/ 2>/dev/null | grep -q DSI; then
        warn "No DSI display detected. Check /boot/firmware/config.txt (see README, section Display)."
    fi
fi

# ----------------------------------------------------------------- packages
log "Installing packages"
# polkitd: lets the netpanel user talk to NetworkManager (rule below)
# nftables: NAT for the Wi-Fi access point (ipv4.method shared); the nftables
#           *service* stays disabled, only the nft tool is used by NetworkManager.
PKGS=(python3 iw network-manager dnsmasq-base polkitd nftables sudo)
if [ "$KIOSK" = 1 ]; then
    PKGS+=(cage fonts-dejavu-core)
    if ! command -v chromium-browser >/dev/null && ! command -v chromium >/dev/null; then
        CHROMIUM=""
        for p in chromium-browser chromium; do
            cand="$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/ {print $2}')"
            if [ -n "$cand" ] && [ "$cand" != "(none)" ]; then CHROMIUM="$p"; break; fi
        done
        [ -n "$CHROMIUM" ] || die "no Chromium package found in apt sources"
        PKGS+=("$CHROMIUM")
    fi
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${PKGS[@]}"

# ----------------------------------------------------------------- user
if ! id "$APP_USER" >/dev/null 2>&1; then
    log "Creating user '$APP_USER' (no password, no login shell)"
    useradd --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin \
            --comment "NetPanel kiosk" "$APP_USER"
fi
for g in video render input netdev; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$APP_USER"
done

# ----------------------------------------------------------------- application
install -d -m 0755 "$APP_DIR"
if [ "$SRC_DIR" -ef "$APP_DIR" ]; then
    log "Running from $APP_DIR itself - keeping installed files (repair mode)"
else
    log "Installing application to $APP_DIR"
    rm -rf "$APP_DIR/netpanel" "$APP_DIR/web" "$APP_DIR/deploy"
    cp -r "$SRC_DIR/netpanel" "$SRC_DIR/web" "$SRC_DIR/deploy" "$APP_DIR/"
    if [ -f "$SRC_DIR/README.md" ]; then cp "$SRC_DIR/README.md" "$APP_DIR/"; fi
fi
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"
chmod 0755 "$APP_DIR/deploy/kiosk.sh"

# ----------------------------------------------------------------- privileges
log "Installing Tailscale helper + sudoers rule"
install -m 0755 -o root -g root "$SRC_DIR/deploy/netpanel-ts-routes" /usr/local/sbin/netpanel-ts-routes
tmp="$(mktemp)"
cp "$SRC_DIR/deploy/netpanel.sudoers" "$tmp"
visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "sudoers syntax check failed"; }
install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/netpanel
rm -f "$tmp"

log "Installing polkit rule for NetworkManager"
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$SRC_DIR/deploy/50-netpanel.rules" /etc/polkit-1/rules.d/50-netpanel.rules
if [ -d /etc/polkit-1/localauthority ]; then
    install -d /etc/polkit-1/localauthority/50-local.d
    install -m 0644 "$SRC_DIR/deploy/50-netpanel.pkla" /etc/polkit-1/localauthority/50-local.d/50-netpanel.pkla
fi
systemctl try-restart polkit.service 2>/dev/null || true

install -m 0644 "$SRC_DIR/deploy/netpanel.pam" /etc/pam.d/netpanel
[ -f /etc/default/netpanel ] || install -m 0644 "$SRC_DIR/deploy/netpanel.default" /etc/default/netpanel

# ----------------------------------------------------------------- sysctl / wifi
if [ "$IPFWD" = 1 ]; then
    log "Enabling IP forwarding (needed for Tailscale subnet routes)"
    install -m 0644 "$SRC_DIR/deploy/99-netpanel-ipforward.conf" /etc/sysctl.d/99-netpanel-ipforward.conf
    sysctl -q -p /etc/sysctl.d/99-netpanel-ipforward.conf
fi

if [ -n "$COUNTRY" ]; then
    log "Setting Wi-Fi country to $COUNTRY"
    raspi-config nonint do_wifi_country "$COUNTRY"
fi
if command -v iw >/dev/null && iw reg get 2>/dev/null | grep -q 'country 00'; then
    warn "Wi-Fi country is not set -> Wi-Fi may stay blocked. Re-run with --wifi-country CZ"
fi

command -v tailscale >/dev/null || warn "Tailscale is not installed yet (see README, section Tailscale)."

# ----------------------------------------------------------------- services
log "Installing systemd units"
install -m 0644 "$SRC_DIR/deploy/netpanel.service" /etc/systemd/system/netpanel.service
install -m 0644 "$SRC_DIR/deploy/netpanel-kiosk.service" /etc/systemd/system/netpanel-kiosk.service
systemctl daemon-reload
systemctl enable netpanel.service
systemctl restart netpanel.service

if [ "$KIOSK" = 1 ]; then
    if [ "$(systemctl get-default)" = "graphical.target" ]; then
        log "Boot target: desktop -> console (the kiosk replaces the desktop on the display)"
        systemctl set-default multi-user.target
    fi
    systemctl disable getty@tty1.service >/dev/null 2>&1 || true
    systemctl enable netpanel-kiosk.service
fi

sleep 1
if systemctl is-active --quiet netpanel.service; then
    log "netpanel.service is running (http://127.0.0.1:8088, local only)"
else
    warn "netpanel.service failed - check: journalctl -u netpanel -e"
fi

echo
log "Done."
if [ "$KIOSK" = 1 ]; then
    echo "    Reboot to start the kiosk:   sudo reboot"
    echo "    (Not started now on purpose: it takes over tty1 and the display.)"
fi
