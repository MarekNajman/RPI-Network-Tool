#!/usr/bin/python3
"""Switch the Raspberry Pi 4 display to the KMS driver (needed by the cage kiosk).

    sudo ./deploy/display-kms.py            dry run: show what would change
    sudo ./deploy/display-kms.py --apply    back up config.txt, apply, then reboot
    sudo ./deploy/display-kms.py --restore  put the backup back, then reboot

What it does in /boot/firmware/config.txt:
  * comments out `dtoverlay=vc4-fkms-v3d` (the old "fake KMS" driver conflicts with KMS)
  * adds, in a final [all] section, whatever is missing for a Pi 4:
        dtoverlay=vc4-kms-v3d          KMS graphics driver
        dtoverlay=vc4-kms-dsi-7inch    Waveshare 5inch DSI LCD (per Waveshare wiki, Pi 4 / DSI1)
  * lines already active for the Pi 4 are left alone; lines that sit under a
    filter that does not match a Pi 4 (e.g. [pi5], [cm4]) do not count.

The first backup is kept as config.txt.netpanel-bak and never overwritten.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import sys

MARK = "# --- added by NetPanel display-kms.py: KMS driver for the Waveshare 5inch DSI LCD ---"
# Section filters that apply to a Raspberry Pi 4 (anything else is treated as "not for us").
PI4_FILTERS = {"all", "pi4", "pi400"}
LEGACY_ONLY = ("lcd_rotate", "display_lcd_rotate", "display_rotate", "framebuffer_width",
               "framebuffer_height", "ignore_lcd", "disable_fw_kms_setup")


def default_path() -> str:
    for p in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.exists(p):
            return p
    return "/boot/firmware/config.txt"


def overlay_name(line: str):
    m = re.match(r"\s*dtoverlay\s*=\s*([^,\s#]+)", line)
    return m.group(1) if m else None


def analyse(lines):
    """Return (active_overlays_for_pi4, fkms_line_indexes, legacy_option_warnings)."""
    active = set()
    fkms = []
    legacy = []
    section = "all"  # lines before the first filter apply to every model
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        applies = section in PI4_FILTERS
        name = overlay_name(line)
        if name == "vc4-fkms-v3d":
            fkms.append(i)
        elif name and applies:
            active.add(name)
        key = line.split("=", 1)[0].strip()
        if applies and key in LEGACY_ONLY:
            legacy.append(line)
    return active, fkms, legacy


def plan(text: str):
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    active, fkms, legacy = analyse(lines)
    new = list(lines)
    for i in fkms:
        new[i] = "#" + new[i].rstrip("\n") + "   # disabled by NetPanel (conflicts with KMS)\n"
    add = []
    if "vc4-kms-v3d" not in active:
        add.append("dtoverlay=vc4-kms-v3d\n")
    if not any(o.startswith("vc4-kms-dsi") for o in active):
        add.append("dtoverlay=vc4-kms-dsi-7inch\n")
    if add:
        # Always open a fresh [all] section: appending under e.g. [cm5] would be ignored by a Pi 4.
        new += ["\n", MARK + "\n", "[all]\n"] + add
    return "".join(new), add, fkms, legacy


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--file=")]
    path = next((a.split("=", 1)[1] for a in argv if a.startswith("--file=")), default_path())
    backup = path + ".netpanel-bak"
    apply = "--apply" in args
    restore = "--restore" in args
    if (apply or restore) and os.geteuid() != 0:
        print("Run as root: sudo %s %s" % (sys.argv[0], " ".join(args)), file=sys.stderr)
        return 1

    if restore:
        if not os.path.exists(backup):
            print("No backup %s found - nothing to restore." % backup, file=sys.stderr)
            return 1
        shutil.copy2(backup, path)
        print("Restored %s from %s. Reboot now: sudo reboot" % (path, backup))
        return 0

    with open(path) as f:
        old = f.read()
    new, added, fkms, legacy = plan(old)

    if new == old:
        print("%s already enables KMS + the DSI panel for the Pi 4. Nothing to change." % path)
        print("If /sys/class/drm still shows no card after a reboot, run deploy/diagnose.sh.")
        return 0

    sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path + " (new)"))
    print()
    if legacy:
        print("NOTE: these legacy-only options stop working under KMS: " + ", ".join(legacy))
    if not apply:
        print("Dry run - nothing written. Apply with:  sudo %s --apply" % sys.argv[0])
        return 0

    if not os.path.exists(backup):
        shutil.copy2(path, backup)
        print("Backup: %s" % backup)
    tmp = path + ".netpanel-tmp"
    with open(tmp, "w") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print("Written. Now: sudo reboot")
    print("After reboot check:  ls /sys/class/drm    (expect e.g. card1-DSI-1)")
    print("Black screen? Over SSH:  sudo %s --restore && sudo reboot" % sys.argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
