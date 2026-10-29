"""Windows: the user's Desktop, Documents and Downloads are the folders the shell reports.

OneDrive's folder backup and the known-folder-move policy put the real Desktop somewhere like
`%USERPROFILE%\\OneDrive\\Desktop`, so `Path.home() / "Desktop"` names a folder the user no longer
looks in. Owner decision (2026-10-29): on Windows the safe machine folders follow
SHGetKnownFolderPath, and a known folder that points at a share, a device or a network drive is
never used. The shell's answer is also not used when the home was redirected (HOME/USERPROFILE
naming another folder than the shell's own profile, as tests and portable setups do), since the
redirect is the caller saying which home it means. Whenever the shell's answer is not used, the
profile spelling `home / name` stays, as before. Off Windows nothing changes.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

from core import windows_path_policy

#: Patched by tests to drive the Windows decision on any platform.
_IS_WINDOWS = os.name == "nt"

_FOLDER_IDS = {
    "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}
#: GetDriveTypeW answers for a drive on this machine: removable, fixed, RAM disk. DRIVE_REMOTE (4),
#: CD-ROM, "unknown" and "no root directory" are not.
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 6})
_DRIVE_REMOTE = 4


def _shell_known_folder(name: str) -> str | None:
    """SHGetKnownFolderPath for one of `_FOLDER_IDS`, or None when the shell cannot answer."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                        ("data4", ctypes.c_ubyte * 8)]

        known = uuid.UUID(_FOLDER_IDS[name])
        guid = _Guid(known.fields[0], known.fields[1], known.fields[2], (ctypes.c_ubyte * 8)(*known.bytes[8:]))
        found = ctypes.c_wchar_p()
        try:
            result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found))
            if result == 0 and found.value:
                return str(found.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(found)  # owed even when the call fails
    except (AttributeError, KeyError, OSError, ValueError):
        pass
    return None


def _drive_is_remote(drive: str) -> bool | None:
    """Whether `drive` (`C:`) is a network drive; None when Windows cannot say, which callers treat
    as remote."""
    try:
        import ctypes

        kind = int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\"))
    except (AttributeError, OSError, ValueError):
        return None
    if kind == _DRIVE_REMOTE:
        return True
    return False if kind in _LOCAL_DRIVE_TYPES else None


def _full_local_path(value: str) -> bool:
    """A full path on a drive letter (`C:\\x`), judged on its spelling: no share, device, root- or
    drive-relative form, and none after `%VAR%` expansion either."""
    return not windows_path_policy.classify(value) and ntpath.isabs(value) and bool(ntpath.splitdrive(value)[0])


def _same_folder(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def safe_folder(name: str, home: Path) -> Path:
    """The user's `name` folder (Desktop, Documents or Downloads) for a machine lane rooted at `home`."""
    fallback = home / name
    if not _IS_WINDOWS or name not in _FOLDER_IDS or name == "Profile":
        return fallback
    profile = _shell_known_folder("Profile")
    if not profile or not _same_folder(str(home), profile):
        return fallback
    reported = _shell_known_folder(name)
    if not reported or not _full_local_path(reported):
        return fallback
    drive = ntpath.splitdrive(reported)[0]
    if drive and _drive_is_remote(drive) is not False:
        return fallback
    return Path(reported)
