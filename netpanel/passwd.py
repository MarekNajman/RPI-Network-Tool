"""Set the NetPanel admin PIN (run as root):  sudo netpanel-passwd

    netpanel-passwd            ask for the new PIN twice (input hidden)
    netpanel-passwd --status   show whether a PIN is set
    netpanel-passwd --remove   delete the PIN -> the panel stays locked
    netpanel-passwd --unlock   clear a brute-force lock (wrong PIN counter)

The hash is stored in /etc/netpanel/auth.json (root:netpanel, mode 0640).
Running sessions are closed automatically when the PIN changes.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import grp
import json
import os
import sys

from .auth import hash_pin, pin_policy_error

AUTH_FILE = os.environ.get("NETPANEL_AUTH_FILE", "/etc/netpanel/auth.json")
STATE_FILE = os.environ.get("NETPANEL_AUTH_STATE", "/var/lib/netpanel/auth-state.json")
GROUP = "netpanel"


def _write_record(path: str, record: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o750, exist_ok=True)
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        gid = -1
    if gid != -1 and os.geteuid() == 0:
        os.chown(d, 0, gid)
        os.chmod(d, 0o750)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    if gid != -1 and os.geteuid() == 0:
        os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def _ask_pin() -> str:
    for _ in range(3):
        pin = getpass.getpass("New NetPanel PIN (6-12 digits): ").strip()
        err = pin_policy_error(pin)
        if err:
            print("  " + err, file=sys.stderr)
            continue
        if getpass.getpass("Repeat PIN: ").strip() != pin:
            print("  PINs do not match", file=sys.stderr)
            continue
        return pin
    raise SystemExit("PIN not changed.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="netpanel-passwd", description="Set the NetPanel admin PIN")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="show whether a PIN is set")
    g.add_argument("--remove", action="store_true", help="delete the PIN (panel stays locked)")
    g.add_argument("--unlock", action="store_true", help="reset the wrong-PIN lock")
    p.add_argument("--file", default=AUTH_FILE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.status:
        try:
            with open(args.file) as f:
                rec = json.load(f)
            print("PIN is set (changed %s)." % rec.get("updated", "?"))
        except (OSError, ValueError):
            print("No PIN set - the panel settings are locked.")
        return 0

    if os.geteuid() != 0 and not os.environ.get("NETPANEL_AUTH_FILE"):
        print("Run as root: sudo netpanel-passwd", file=sys.stderr)
        return 1

    if args.remove:
        try:
            os.remove(args.file)
            print("PIN removed. The panel settings are locked until a new PIN is set.")
        except FileNotFoundError:
            print("No PIN was set.")
        return 0

    if args.unlock:
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
        print("Wrong-PIN lock cleared. Restart the service to apply: sudo systemctl restart netpanel")
        return 0

    pin = _ask_pin()
    record = hash_pin(pin)
    record["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write_record(args.file, record)
    print("PIN saved to %s. Open sessions on the panel were closed." % args.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
