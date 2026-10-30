"""Tie a child process tree to this process's lifetime on Windows.

A child is started with ``CREATE_SUSPENDED``, put in a Job object with KILL_ON_JOB_CLOSE whose only
handle this process holds, and only then resumed. It cannot start anything before it is in the
job, and everything it starts afterwards is born inside it, so when this process ends, however it
ends, Windows closes the handle and ends the whole tree. Used by both bundle supervisors (G4 orphan
check on Steve, 177621c: a hard kill of the supervisor left the API holding port 11435).
"""
from __future__ import annotations

from typing import Any

#: ``subprocess`` has no name for it; the documented process-creation flag.
CREATE_SUSPENDED = 0x00000004


def new_kill_on_close_job() -> Any:
    """A Job object with KILL_ON_JOB_CLOSE whose only handle this process holds (not inherited)."""
    from core.windows_broker.process import _WindowsJob

    return _WindowsJob()


def resume_suspended(pid: int) -> None:
    """Resume every thread of a process started with ``CREATE_SUSPENDED`` (it has exactly one)."""
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    th32cs_snapthread = 0x00000004
    thread_suspend_resume = 0x0002
    invalid_handle = wintypes.HANDLE(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
    if not snapshot or snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(_ThreadEntry)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(thread_suspend_resume, False, entry.th32ThreadID)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    resumed += 1
                finally:
                    kernel32.CloseHandle(thread)
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f"no thread of process {pid} was found to resume")
