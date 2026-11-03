"""Refuse a folder to this user on Windows, the way chmod refuses it on POSIX.

Windows has no directory mode bits: chmod(0) or chmod(0o555) on a folder only toggles the
read-only attribute, which neither stops a listing nor a write inside it. A test that needs the
folder really refused adds a deny entry for the current user with System32's icacls, and the
entry is removed again on the way out. Deny entries apply to administrators too.
"""
from __future__ import annotations

import contextlib
import csv
import ntpath
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

LIST = "(OI)(CI)(RD)"  # list the folder / read its files
WRITE = "(OI)(CI)(WD,AD)"  # create files / create folders inside it


def _system32(tool: str) -> str:
    return ntpath.join(os.environ["SystemRoot"], "System32", tool)


def _current_user_sid() -> str:
    out = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    ).stdout
    return next(csv.reader(out.splitlines()))[1]


@contextlib.contextmanager
def refused(path: Path, rights: str) -> Iterator[None]:
    """Deny ``rights`` on ``path`` to the current user for the body of the ``with``."""
    if os.name != "nt":
        raise RuntimeError("refused() adds a Windows ACL deny entry; use chmod on POSIX")
    icacls = _system32("icacls.exe")
    who = "*" + _current_user_sid()
    subprocess.run([icacls, str(path), "/deny", f"{who}:{rights}"], capture_output=True, check=True)
    try:
        yield
    finally:
        subprocess.run([icacls, str(path), "/remove:d", who], capture_output=True, check=True)
