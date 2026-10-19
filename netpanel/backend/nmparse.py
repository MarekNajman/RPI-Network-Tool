"""Parsers for nmcli terse output (`nmcli -t ...`).

In terse mode nmcli separates fields with ':' and escapes literal ':' and '\\'
inside values with a backslash (e.g. MAC "DC\\:A6\\:32\\:01\\:02\\:03").
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional


def split_terse(line: str) -> List[str]:
    """Split one terse line into fields, honouring backslash escapes."""
    fields: List[str] = []
    cur: List[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_kv(output: str) -> Dict[str, str]:
    """Parse `nmcli -e no -t -f ... device show` / `connection show <id>` output.

    Run with `--escape no`, so each line is KEY:VALUE with the value verbatim
    (it may contain ':' or '\\'). Keys never contain ':', so the first colon
    separates them. Indexed keys (IP4.ADDRESS[1]) are kept as-is.
    """
    result: Dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            result[key] = value
    return result


def collect(kv: Dict[str, str], prefix: str) -> List[str]:
    """Return values of KEY[1], KEY[2]... (or plain KEY) in index order."""
    items = []
    for k, v in kv.items():
        if k == prefix:
            items.append((0, v))
            continue
        m = re.match(re.escape(prefix) + r"\[(\d+)\]$", k)
        if m:
            items.append((int(m.group(1)), v))
    return [v for _, v in sorted(items) if not is_empty(v)]


def is_empty(value: Optional[str]) -> bool:
    return value is None or value.strip() in ("", "--")


def clean(value: Optional[str]) -> str:
    return "" if is_empty(value) else value.strip()


def raw(value: Optional[str]) -> str:
    """Like clean() but keeps surrounding spaces (SSIDs/passwords may have them)."""
    return "" if value is None or value in ("", "--") else value


def state_name(value: Optional[str]) -> str:
    """'100 (connected)' -> 'connected'; 'connected (externally)' -> 'connected'."""
    v = clean(value)
    m = re.search(r"\(([^)]*)\)", v)
    if m and v[:1].isdigit():
        v = m.group(1)
    return v.split(" ")[0] if v else "unknown"


def split_list(value: Optional[str]) -> List[str]:
    """Split comma/space separated property values ('1.1.1.1,8.8.8.8')."""
    v = clean(value)
    if not v:
        return []
    return [x for x in re.split(r"[,\s]+", v) if x]


def split_cidr(value: str):
    """'192.168.1.5/24' -> ('192.168.1.5', 24)."""
    addr, _, pfx = clean(value).partition("/")
    try:
        return addr, int(pfx) if pfx else None
    except ValueError:
        return addr, None


def parse_freq(value: str) -> int:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else 0


def classify_security(sec: str) -> str:
    s = (sec or "").upper().strip()
    if s in ("", "--"):
        return "open"
    if "802.1X" in s:
        return "enterprise"
    if "WEP" in s:
        return "wep"
    has_wpa12 = "WPA2" in s or "WPA1" in s
    if "WPA3" in s and not has_wpa12:
        return "sae"
    if has_wpa12:
        return "wpa-psk"
    if "OWE" in s:
        return "owe"
    return "unsupported"
