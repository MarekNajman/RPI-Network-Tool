#!/bin/bash
# Check - and optionally repair - every privilege NetPanel needs.
#
#   sudo ./deploy/check-permissions.sh          check only (changes nothing)
#   sudo ./deploy/check-permissions.sh --fix    reinstall polkit rule + sudo rule, restart polkit, check again
#
# Typical symptom this answers: "Insufficient privileges" / "Not authorized"
# when saving Wi-Fi / Ethernet settings on the panel.
set -u
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 ${1:-}" >&2; exit 1; }

U=netpanel
DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE=/etc/polkit-1/rules.d/10-netpanel.rules
OLD_RULE=/etc/polkit-1/rules.d/50-netpanel.rules
PKLA_DIR=/etc/polkit-1/localauthority/50-local.d
FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

FAILS=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
info() { printf '       %s\n' "$*"; }

check() {
    FAILS=0
    id "$U" >/dev/null 2>&1 || { fail "user '$U' does not exist - run deploy/install.sh"; return; }

    echo "== 1. polkit daemon (NetworkManager asks it whether '$U' may change the network)"
    if systemctl is-active --quiet polkit.service; then
        pass "polkit.service is running ($(pkaction --version 2>/dev/null || echo 'version unknown'))"
    else
        fail "polkit.service is NOT running -> NetworkManager refuses every non-root change"
        info "fix: sudo apt install polkitd && sudo systemctl enable --now polkit"
    fi

    echo "== 2. NetPanel polkit rule"
    if [ -r "$RULE" ]; then pass "$RULE present"; else fail "$RULE missing"; fi
    [ -e "$OLD_RULE" ] && warn "old $OLD_RULE still present (harmless; --fix removes it)"
    local other
    other="$(grep -lsE 'NetworkManager' /etc/polkit-1/rules.d/*.rules /usr/share/polkit-1/rules.d/*.rules 2>/dev/null | grep -v netpanel | tr '\n' ' ')"
    [ -n "$other" ] && info "other rules that mention NetworkManager (evaluated in file-name order): $other"
    local errs
    errs="$(journalctl -b -u polkit.service --no-pager 2>/dev/null | grep -iE 'error|exception|syntax' | tail -n 3)"
    if [ -n "$errs" ]; then
        warn "polkit logged errors this boot (a broken .rules file is ignored):"
        printf '%s\n' "$errs" | sed 's/^/         /'
    fi

    echo "== 3. NetworkManager authorization mode"
    local ap
    ap="$(NetworkManager --print-config 2>/dev/null | sed -n 's/^auth-polkit=//p' | tail -n 1)"
    case "${ap:-true}" in
        true|yes) pass "auth-polkit=${ap:-true (default)}" ;;
        *) fail "auth-polkit=$ap -> NetworkManager ignores polkit and only lets root change the network"
           info "fix: remove the auth-polkit line from /etc/NetworkManager/NetworkManager.conf (and conf.d/*.conf),"
           info "     then: sudo systemctl restart NetworkManager   (network drops for a few seconds)" ;;
    esac

    echo "== 4. Effective NetworkManager permissions of user '$U' (this is what the panel gets)"
    local perms p v
    perms="$(sudo -u "$U" nmcli -t -f PERMISSION,VALUE general permissions 2>&1)"
    for p in settings.modify.system network-control enable-disable-wifi wifi.scan wifi.share.protected wifi.share.open; do
        v="$(printf '%s\n' "$perms" | sed -n "s/^org\.freedesktop\.NetworkManager\.$p://p")"
        if [ "$v" = "yes" ]; then pass "$p: yes"
        else fail "$p: ${v:-?}   (yes = allowed, auth/no = polkit rule not effective)"; fi
    done

    echo "== 5. Tailscale helper via sudo"
    if sudo -u "$U" sudo -n -l /usr/local/sbin/netpanel-ts-routes >/dev/null 2>&1; then
        pass "'$U' may run /usr/local/sbin/netpanel-ts-routes"
    else
        fail "sudo rule missing or broken (/etc/sudoers.d/netpanel)"
    fi

    echo "== 6. Admin PIN"
    if [ -f /etc/netpanel/auth.json ]; then
        if sudo -u "$U" test -r /etc/netpanel/auth.json; then pass "PIN set and readable by '$U'"
        else fail "/etc/netpanel/auth.json not readable by '$U' (expected root:$U 0640)"; fi
    else
        warn "no PIN set yet: sudo netpanel-passwd"
    fi

    echo "== 7. API service"
    local pid owner
    pid="$(systemctl show -p MainPID --value netpanel.service 2>/dev/null)"
    owner="$( [ -n "$pid" ] && [ "$pid" != 0 ] && ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [ "$owner" = "$U" ]; then pass "netpanel.service runs as '$U' (pid $pid)"
    else fail "netpanel.service not running as '$U' (pid=${pid:-none}, user=${owner:-?})"; fi
}

repair() {
    echo "== Repair"
    if ! command -v pkaction >/dev/null; then
        echo "   installing polkitd"
        apt-get install -y polkitd
    fi
    install -d -m 0755 /etc/polkit-1/rules.d
    install -m 0644 "$DEPLOY/10-netpanel.rules" "$RULE"
    rm -f "$OLD_RULE"
    # Legacy .pkla format as well (used when polkitd-pkla / old polkit is in charge).
    install -d "$PKLA_DIR"
    install -m 0644 "$DEPLOY/50-netpanel.pkla" "$PKLA_DIR/50-netpanel.pkla"
    systemctl enable polkit.service >/dev/null 2>&1 || true
    systemctl restart polkit.service
    echo "   polkit rule reinstalled, polkit restarted"
    if [ -f "$DEPLOY/netpanel-ts-routes" ] && [ -f "$DEPLOY/netpanel.sudoers" ]; then
        install -m 0755 -o root -g root "$DEPLOY/netpanel-ts-routes" /usr/local/sbin/netpanel-ts-routes
        local tmp
        tmp="$(mktemp)"
        cp "$DEPLOY/netpanel.sudoers" "$tmp"
        if visudo -cf "$tmp" >/dev/null; then
            install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/netpanel
            echo "   sudo rule reinstalled"
        fi
        rm -f "$tmp"
    fi
    [ -f /etc/netpanel/auth.json ] && chown root:"$U" /etc/netpanel/auth.json && chmod 0640 /etc/netpanel/auth.json
    sleep 1
    echo
}

[ "$FIX" = 1 ] && repair
check
echo
if [ "$FAILS" = 0 ]; then
    echo "All checks passed. If the panel still says 'Insufficient privileges', send the output of:"
    echo "   journalctl -b -u netpanel -u polkit -u NetworkManager --no-pager | tail -n 80"
    exit 0
fi
echo "$FAILS check(s) failed."
[ "$FIX" = 0 ] && echo "Try the automatic repair:  sudo $0 --fix"
exit 1
