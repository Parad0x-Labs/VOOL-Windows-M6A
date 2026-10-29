"""Windows' own tools for tests that pin the Windows platform seam off Windows.

VOOL runs Windows' own tools (taskkill, PowerShell, schtasks, tasklist, sc, cmd, wevtutil) only
from ``%SystemRoot%\\System32`` (core/process_liveness.py) and fails closed when that folder can't be trusted. A test that drives
the Windows path on another host gives it a Windows folder whose tools exist, so the code under
test runs the same commands it runs on Windows; nothing is started.
"""
from __future__ import annotations

import ntpath

import pytest

from core import process_liveness

SYSTEM_ROOT = "C:\\Windows"
TASKKILL = ntpath.join(SYSTEM_ROOT, "System32", "taskkill.exe")
POWERSHELL = ntpath.join(SYSTEM_ROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
SCHTASKS = ntpath.join(SYSTEM_ROOT, "System32", "schtasks.exe")
TASKLIST = ntpath.join(SYSTEM_ROOT, "System32", "tasklist.exe")
SC = ntpath.join(SYSTEM_ROOT, "System32", "sc.exe")
CMD = ntpath.join(SYSTEM_ROOT, "System32", "cmd.exe")
WEVTUTIL = ntpath.join(SYSTEM_ROOT, "System32", "wevtutil.exe")
_TOOLS = (TASKKILL, POWERSHELL, SCHTASKS, TASKLIST, SC, CMD, WEVTUTIL)


def trust_system32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", SYSTEM_ROOT)
    real = process_liveness.os.path.isfile
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda path: path in _TOOLS or real(path))


def program(argv0: str) -> str:
    """The program's file name, as Windows reads the path (backslashes included)."""
    return ntpath.basename(argv0)
