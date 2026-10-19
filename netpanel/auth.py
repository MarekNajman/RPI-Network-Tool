"""Admin PIN authentication for the touch panel.

* The PIN hash lives in /etc/netpanel/auth.json (root:netpanel 0640), written
  only by `sudo netpanel-passwd` - the panel itself can never change it.
* PBKDF2-HMAC-SHA256 with a random salt, constant-time comparison.
* Brute-force protection: after 5 wrong PINs the login is locked for 60 s,
  every further failed round doubles the lock (max 15 min). The counter is
  persisted, so restarting the service does not reset it.
* Sessions are random tokens kept only in memory (a service restart logs
  everybody out). A session expires after `idle_timeout` seconds without an
  authenticated request; the UI enforces the 5 min inactivity logout itself
  and pings while the user is active, the server is the backstop.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger("netpanel.auth")

ALGO = "pbkdf2_sha256"
ITERATIONS = 150_000
PIN_MIN, PIN_MAX = 6, 12
MAX_FAILURES = 5          # wrong PINs before a lock
LOCK_BASE_S = 60          # first lock duration
LOCK_MAX_S = 15 * 60      # cap
MAX_SESSIONS = 8
SERVER_GRACE_S = 45       # server keeps a session this much longer than the UI


class AuthError(Exception):
    def __init__(self, message: str, retry_after: int = 0, attempts_left: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        self.attempts_left = attempts_left


# --------------------------------------------------------------------------- hashing


def hash_pin(pin: str, iterations: int = ITERATIONS, salt: Optional[bytes] = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return {
        "version": 1,
        "algo": ALGO,
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify_pin(pin: str, record: dict) -> bool:
    try:
        if record.get("algo") != ALGO:
            return False
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def pin_policy_error(pin: str) -> Optional[str]:
    """Rules for a new PIN (enforced by netpanel-passwd)."""
    if not pin.isdigit() or not (PIN_MIN <= len(pin) <= PIN_MAX):
        return "PIN must have %d-%d digits" % (PIN_MIN, PIN_MAX)
    if len(set(pin)) == 1:
        return "PIN must not be one repeated digit"
    steps = {(int(b) - int(a)) % 10 for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {9}):
        return "PIN must not be a simple sequence like 123456"
    return None


# --------------------------------------------------------------------------- manager


class AuthManager:
    def __init__(
        self,
        auth_file: Optional[str],
        state_file: Optional[str] = None,
        idle_timeout: int = 300,
        record: Optional[dict] = None,
        clock: Callable[[], float] = time.time,
    ):
        """`record` (an in-memory hash) is used by demo mode instead of a file."""
        self.auth_file = auth_file
        self.state_file = state_file
        self.idle_timeout = int(idle_timeout)
        self._static_record = record
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}  # token -> last activity
        self._record_sig = None
        self._failures = 0
        self._lock_level = 0
        self._locked_until = 0.0
        self._load_state()

    # ------------------------------------------------------------- persistence

    def _read_record(self) -> Optional[dict]:
        if self._static_record is not None:
            return self._static_record
        if not self.auth_file:
            return None
        try:
            st = os.stat(self.auth_file)
            with open(self.auth_file) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        # PIN changed on disk -> drop all sessions.
        sig = (st.st_mtime_ns, st.st_size)
        if self._record_sig is not None and sig != self._record_sig and self._sessions:
            log.info("PIN file changed - all sessions closed")
            self._sessions.clear()
        self._record_sig = sig
        return rec if isinstance(rec, dict) and rec.get("hash") else None

    def _load_state(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file) as f:
                s = json.load(f)
            self._failures = int(s.get("failures", 0))
            self._lock_level = int(s.get("lock_level", 0))
            self._locked_until = float(s.get("locked_until", 0))
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        data = {"failures": self._failures, "lock_level": self._lock_level, "locked_until": self._locked_until}
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("cannot persist login state: %s", e)

    # ------------------------------------------------------------- sessions

    def _expired(self, last: float, now: float) -> bool:
        return now - last > self.idle_timeout + SERVER_GRACE_S

    def _purge(self, now: float) -> None:
        for tok in [t for t, last in self._sessions.items() if self._expired(last, now)]:
            del self._sessions[tok]

    def configured(self) -> bool:
        with self._lock:
            return self._read_record() is not None

    def check(self, token: Optional[str], touch: bool = True) -> bool:
        if not token:
            return False
        with self._lock:
            self._read_record()  # notices PIN changes
            now = self._clock()
            last = self._sessions.get(token)
            if last is None:
                return False
            if self._expired(last, now):
                del self._sessions[token]
                return False
            if touch:
                self._sessions[token] = now
            return True

    def logout(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def login(self, pin: str) -> str:
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                wait = int(self._locked_until - now + 0.999)
                raise AuthError("Too many wrong PINs. Try again in %d s." % wait, retry_after=wait)
            record = self._read_record()
            if record is None:
                raise AuthError("No PIN is set. Set one over SSH: sudo netpanel-passwd")
            pin = str(pin or "")
            ok = pin.isdigit() and len(pin) <= PIN_MAX and verify_pin(pin, record)
            if not ok:
                self._failures += 1
                left = MAX_FAILURES - self._failures
                if left <= 0:
                    duration = min(LOCK_BASE_S * (2 ** self._lock_level), LOCK_MAX_S)
                    self._lock_level += 1
                    self._failures = 0
                    self._locked_until = now + duration
                    self._save_state()
                    log.warning("login locked for %d s after repeated wrong PINs", duration)
                    raise AuthError("Too many wrong PINs. Locked for %d s." % duration, retry_after=duration)
                self._save_state()
                log.info("wrong PIN (%d attempts left)", left)
                raise AuthError("Wrong PIN. %d attempt%s left." % (left, "" if left == 1 else "s"), attempts_left=left)

            self._failures = 0
            self._lock_level = 0
            self._locked_until = 0.0
            self._save_state()
            self._purge(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now
            log.info("admin logged in")
            return token

    def status(self, token: Optional[str]) -> dict:
        logged_in = self.check(token, touch=False)
        with self._lock:
            now = self._clock()
            locked = max(0, int(self._locked_until - now + 0.999)) if now < self._locked_until else 0
            return {
                "configured": self._read_record() is not None,
                "logged_in": logged_in,
                "idle_timeout": self.idle_timeout,
                "locked_for": locked,
            }
