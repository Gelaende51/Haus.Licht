"""The one-time root setup (install/setup-root.sh): its version and running it.

The version lives in one place, install/setup-version (an integer). The
script writes it to STAMP when it has run through; the daemon compares the
two: "missing" (never run), "outdated" (an older run, also one from before
the stamp existed) or "current". History: 1 uleds and its udev rule,
2 the conn-update helper and its sudoers rule, 3 the BlueZ tuning (A4),
the stamp and a bluetoothd restart when the tuning changed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from .common import ROOT

SCRIPT = ROOT / "install" / "setup-root.sh"
VERSION_FILE = ROOT / "install" / "setup-version"
STAMP = Path("/usr/local/lib/hauslicht/setup-version")
# what a run from before the stamp left behind: such a setup is outdated, not missing
LEGACY = (Path("/usr/local/lib/hauslicht/conn-update"), Path("/etc/modules-load.d/haus-licht.conf"))


def required() -> int:
    return int(VERSION_FILE.read_text().strip())


def installed(stamp: Path | None = None) -> int | None:
    try:
        return int((stamp or STAMP).read_text().strip())
    except (OSError, ValueError):
        return None


def status(stamp: Path | None = None, legacy=None) -> dict:
    """{"status": missing|outdated|current, "installed": int|None, "required": int}."""
    want, have = required(), installed(stamp)
    if have is None:
        legacy = LEGACY if legacy is None else legacy
        state = "outdated" if any(p.exists() for p in legacy) else "missing"
    else:
        state = "current" if have >= want else "outdated"
    return {"status": state, "installed": have, "required": want}


def command(tty: bool) -> list[str]:
    """pkexec (a graphical polkit prompt) for the daemon; sudo on a terminal."""
    return (["sudo"] if tty else ["pkexec"]) + [str(SCRIPT)]


async def run(cmd: list[str] | None = None) -> tuple[int, str]:
    """Run the setup; (exit code, last output line). pkexec: 126 the prompt
    was dismissed, 127 no authentication agent or not allowed."""
    cmd = cmd or command(False)
    if shutil.which(cmd[0]) is None:
        return 127, f"{cmd[0]} not found"
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env={**os.environ, "LC_ALL": "C"})
    out, _err = await proc.communicate()
    lines = [ln for ln in out.decode(errors="replace").splitlines() if ln.strip()]
    return proc.returncode, (lines[-1] if lines else "")


def run_on_tty() -> int:
    """`hauslicht setup-root` on a terminal: sudo, the output shown as it comes."""
    import subprocess
    return subprocess.call(command(True), stdin=sys.stdin)
