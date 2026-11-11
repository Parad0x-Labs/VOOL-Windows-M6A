"""Windows: never run, stat or look up a macOS/Linux program by its POSIX path.

Several Mac features name their program by an absolute POSIX path: ``/usr/bin/python3`` for PDF
reading, ``/usr/bin/osascript`` for Apple Notes, ``/usr/bin/swiftc``, ``/usr/bin/textutil``,
``/usr/bin/bsdtar``, ``/usr/bin/sandbox-exec``, ``/bin/sh``, ``/bin/ps`` and the ffmpeg candidates
under ``/opt/homebrew`` and ``/usr/local``. On macOS that path is a root-owned system file. On
Windows the same string has no drive letter, so Windows resolves it against the current drive:
``/usr/bin/python3`` becomes ``C:\\usr\\bin\\python3.exe``. Any account may create ``C:\\usr``,
``C:\\bin`` and ``C:\\opt``, so whoever plants a program there gets it run as the VOOL user
(CWE-427). An ``os.access`` or ``os.path.exists`` on that path is the same trust decision one step
earlier: it is what lets the plant be chosen.

The rule, on Windows only: a program path is used only when it starts with a drive letter and a
separator (``C:\\...``). Every other spelling (rooted ``/usr/bin/x`` or ``\\bin\\x``, drive-relative
``C:x``, a share ``\\\\host\\x``, a device path) is refused before any filesystem call, and the caller
reports ``missing_dependency``. Off Windows every function answers exactly what the plain ``os`` call
would, so macOS and Linux keep their behaviour.

Tests switch the rule on through ``_IS_WINDOWS``, never through ``os.name`` or ``sys.platform``,
which pathlib branches on.
"""
from __future__ import annotations

import errno
import os
import re
from collections.abc import Sequence
from typing import Any

#: Patched by tests to drive the Windows decision on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"
STATUS = "missing_dependency"

_DRIVE_ROOTED = re.compile(r"^[A-Za-z]:[\\/]")


def active() -> bool:
    """Whether this process applies the rule: on Windows, or when a test switches it on."""
    return _IS_WINDOWS


def refused(path: Any) -> bool:
    """On Windows, True for any program path that does not start with a drive letter and a separator.

    Pure: it reads the spelling only and touches no file. Off Windows, always False."""
    if not _IS_WINDOWS:
        return False
    return not _DRIVE_ROOTED.match(os.fspath(path) if isinstance(path, os.PathLike) else str(path or ""))


def reason(path: Any) -> str:
    """The plain-words reason a refused program path was not used."""
    return (
        f"{path} is a macOS program path; Windows would look for it in a folder any account can "
        f"create, so it was not used and nothing was run"
    )


class PosixProgramRefused(FileNotFoundError):
    """Raised before a spawn on Windows. A FileNotFoundError naming the program, so every caller
    that already reports a missing program reports this one the same way, as missing_dependency."""

    def __init__(self, path: Any) -> None:
        super().__init__(errno.ENOENT, reason(path), str(path))


def check(path: Any) -> str:
    """``path`` as a string when it may be used; raises ``PosixProgramRefused`` when it may not."""
    if refused(path):
        raise PosixProgramRefused(path)
    return os.fspath(path) if isinstance(path, os.PathLike) else str(path)


def argv(command: Sequence[Any]) -> list[Any]:
    """The command unchanged, after checking the program it names. Wrap every spawn of a constant."""
    items = list(command)
    if items:
        check(items[0])
    return items


def executable(path: Any) -> bool:
    """``os.access(path, os.X_OK)``, except that a refused path is never stat'ed and is False."""
    if refused(path):
        return False
    return os.access(path, os.X_OK)


def exists(path: Any) -> bool:
    """``os.path.exists(path)``, except that a refused path is never stat'ed and is False."""
    if refused(path):
        return False
    return os.path.exists(path)


def missing_dependency(path: Any) -> dict[str, str]:
    """The structured result a tool returns when its program path was refused."""
    return {"status": STATUS, "reason": reason(path)}


__all__ = [
    "STATUS",
    "PosixProgramRefused",
    "active",
    "argv",
    "check",
    "executable",
    "exists",
    "missing_dependency",
    "reason",
    "refused",
]
