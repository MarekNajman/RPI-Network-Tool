"""Subprocess wrapper: consistent logging, timeouts, secret redaction, errors."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Iterable, List, Optional, Sequence

log = logging.getLogger("netpanel.cmd")

# Commands are run with a fixed, predictable environment so nmcli prints
# English, machine-parsable output regardless of the system locale.
_BASE_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


class CommandError(Exception):
    """A command failed. `message` is short and suitable for the UI."""

    def __init__(self, cmd: str, returncode: int, stdout: str = "", stderr: str = "", message: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        self.message = message or self._derive_message()
        super().__init__(self.message)

    def _derive_message(self) -> str:
        text = (self.stderr.strip() or self.stdout.strip() or "exit code %d" % self.returncode)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # nmcli prefixes messages with "Error: "; keep the most informative line.
        for ln in lines:
            if ln.lower().startswith("error:"):
                return ln[6:].strip()
        return lines[-1] if lines else text


def redact(args: Sequence[str], secrets: Iterable[str]) -> List[str]:
    secrets = [s for s in secrets if s]
    out = []
    for a in args:
        for s in secrets:
            if s in a:
                a = a.replace(s, "******")
        out.append(a)
    return out


def run(
    args: Sequence[str],
    timeout: float = 20,
    check: bool = True,
    secrets: Iterable[str] = (),
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command (never through a shell). Raises CommandError on failure."""
    secrets = list(secrets)
    shown = " ".join(shlex.quote(a) for a in redact(args, secrets))
    if quiet:
        log.debug("$ %s", shown)
    else:
        log.info("$ %s", shown)
    env = dict(os.environ)
    env.update(_BASE_ENV)
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CommandError(shown, 127, message="%s: command not found" % args[0]) from None
    except subprocess.TimeoutExpired:
        raise CommandError(shown, -1, message="Timed out after %ds: %s" % (timeout, args[0])) from None

    if check and proc.returncode != 0:
        err = CommandError(shown, proc.returncode, proc.stdout, redact([proc.stderr], secrets)[0])
        log.warning("command failed (%d): %s -> %s", proc.returncode, shown, err.message)
        raise err
    return proc


def which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name, path=_BASE_ENV["PATH"])
