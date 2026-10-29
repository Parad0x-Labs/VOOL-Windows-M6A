"""Whether a process exists, asked without signalling it.

`os.kill(pid, 0)` is the POSIX probe. On Windows it is not a probe at all: CPython maps every
signal other than the two console events to TerminateProcess, so the "check" kills the process
with exit code 0. The installed window host's boot-time pidfile sweep "checked" the live API that
way and stopped it two seconds after the window launched. Windows asks the kernel instead.
"""
from __future__ import annotations

import os

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_WAIT_TIMEOUT = 0x102


def pid_alive(pid: int) -> bool:
    """True while ``pid`` names a running process, including one this user may not signal."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # exists but not ours to signal
        return True
    if pid > 0xFFFFFFFF:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists, another account's
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _system32(*parts: str) -> str:
    """A Windows tool under ``%SystemRoot%\\System32`` by absolute path, or OSError.

    A bare tool name is looked up through the app's folder, the current folder and PATH, so a
    planted copy there would run instead. Only ``%SystemRoot%\\System32`` is trusted; a missing,
    relative, drive-less or network-share SystemRoot, or no such file there, fails closed and the
    caller falls back to what it does when the tool cannot run.
    """
    import ntpath

    root = os.environ.get("SystemRoot") or ""
    drive, rest = ntpath.splitdrive(root)
    # A drive letter only: a \\server\share root would run a network copy of the tool.
    if len(drive) != 2 or drive[1] != ":" or not rest.startswith(("\\", "/")):
        raise OSError("SystemRoot is not an absolute Windows folder; Windows tools are not trusted")
    exe = ntpath.join(root, "System32", *parts)
    if not os.path.isfile(exe):
        raise OSError(f"{exe} is missing")
    return exe


def system_taskkill() -> str:
    """Windows' own taskkill.exe by absolute path, or OSError (see ``_system32``)."""
    return _system32("taskkill.exe")


def system_powershell() -> str:
    """Windows PowerShell 5.1's powershell.exe by absolute path, or OSError (see ``_system32``)."""
    return _system32("WindowsPowerShell", "v1.0", "powershell.exe")


SYSTEM32_TOOLS = frozenset({"taskkill", "schtasks", "tasklist", "sc", "cmd", "wevtutil"})


def system_tool(name: str) -> str:
    """One of ``SYSTEM32_TOOLS`` (a bare name such as ``tasklist``) by absolute path, or OSError."""
    if name not in SYSTEM32_TOOLS:
        raise OSError(f"{name} is not a Windows tool VOOL runs")
    return _system32(f"{name}.exe")
