"""Windows: the user's Desktop, Documents and Downloads are the folders the shell reports.

OneDrive's folder backup and the known-folder-move policy put the real Desktop somewhere like
`%USERPROFILE%\\OneDrive\\Desktop`, so `Path.home() / "Desktop"` names a folder the user no longer
looks in. Owner decision (2026-10-29): on Windows the safe machine folders follow
SHGetKnownFolderPath, and a known folder that points at a share, a device or a network drive is
never used, nor is one at a drive root or holding the profile, which would open the whole drive
or profile. The shell's answer is also not used when the home was redirected (HOME/USERPROFILE
naming another folder than the shell's own profile, as tests and portable setups do), since the
redirect is the caller saying which home it means. Whenever the shell's answer is not used, the
profile spelling `home / name` stays, as before. Off Windows nothing changes.
"""
from __future__ import annotations

import ntpath
import os
import threading
from pathlib import Path

from core import windows_path_policy

#: Patched by tests to drive the Windows decision on any platform.
_IS_WINDOWS = os.name == "nt"

_FOLDER_IDS = {
    "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "ProgramFiles": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "ProgramFilesX86": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    # Read by core/windows_disposable_store.py, which refuses to mark these (or what holds them).
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "OneDrive": "{A52BBA46-E9E1-435F-B3D9-28DAA648C0F6}",
    "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "RoamingAppData": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "ProgramData": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    "Windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
}
#: GetDriveTypeW answers for a drive on this machine: removable, fixed, RAM disk. DRIVE_REMOTE (4),
#: CD-ROM, "unknown" and "no root directory" are not.
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 6})
_DRIVE_REMOTE = 4


#: SHGetKnownFolderPath answers that mean "this PC has no such folder" (owner decision D13 A: a
#: missing OneDrive is no reason to refuse a mark): HRESULT_FROM_WIN32 of ERROR_FILE_NOT_FOUND and
#: ERROR_PATH_NOT_FOUND, and E_INVALIDARG ("not present on the system"). Every other failure,
#: E_FAIL included, is a lookup that failed.
_ABSENT_RESULTS = frozenset({0x80070002, 0x80070003, 0x80070057})
#: What the last `known_folder` call on this thread learned, per name (see `outcome`).
_OUTCOMES = threading.local()
FOUND = "found"
ABSENT = "absent"
FAILED = "failed"


def _record(name: str, kind: str) -> None:
    table = getattr(_OUTCOMES, "table", None)
    if table is None:
        table = _OUTCOMES.table = {}
    table[name] = kind


def clear_outcome(name: str) -> None:
    """Forget what an earlier `known_folder(name)` call on this thread learned."""
    getattr(_OUTCOMES, "table", {}).pop(name, None)


def outcome(name: str) -> str:
    """How the last `known_folder(name)` call on this thread ended: FOUND, ABSENT (the shell said
    this PC has no such folder), FAILED or ``failed: <why>`` (the shell could not answer, or
    answered with a folder that is not a full local path), or '' when nothing was asked (off
    Windows, or a name this module does not know)."""
    return str(getattr(_OUTCOMES, "table", {}).get(name, ""))


def _shell_known_folder(name: str) -> str | None:
    """SHGetKnownFolderPath for one of `_FOLDER_IDS`, or None when the shell cannot answer. Why it
    could not is noted for `known_folder` (ABSENT only when the shell said there is no such folder)."""
    _record(name, FAILED)
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
                _record(name, FOUND)
                return str(found.value)
            code = int(result) & 0xFFFFFFFF
            _record(name, ABSENT if code in _ABSENT_RESULTS else f"{FAILED}: shell answer 0x{code:08X}")
        finally:
            ctypes.windll.ole32.CoTaskMemFree(found)  # owed even when the call fails
    except (AttributeError, KeyError, OSError, ValueError) as exc:
        _record(name, f"{FAILED}: {type(exc).__name__}")
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


def _resolved(value: str) -> str:
    """Where `value` really leads. Every caller resolves the folder before using it, so a junction
    (`mklink /J` needs no admin) is judged by its target, not its own spelling."""
    return os.path.realpath(value)


def _usable(value: str, profile: str) -> bool:
    if not _full_local_path(value) or _too_wide(value, profile):
        return False
    drive = ntpath.splitdrive(value)[0]
    return not drive or _drive_is_remote(drive) is False


def _norm(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _same_folder(left: str, right: str) -> bool:
    return _norm(left) == _norm(right)


def _too_wide(reported: str, profile: str) -> bool:
    """A drive root, the profile itself or a folder holding it: as a write root it would open the
    whole drive or profile, so it is never used."""
    folder = _norm(reported)
    if ntpath.splitdrive(folder)[1] in ("", "\\"):
        return True
    held = _norm(profile)
    return held == folder or held.startswith(folder.rstrip("\\") + "\\")


def safe_folder(name: str, home: Path) -> Path:
    """The user's `name` folder (Desktop, Documents or Downloads) for a machine lane rooted at `home`."""
    fallback = home / name
    if not _IS_WINDOWS or name not in ("Desktop", "Documents", "Downloads"):
        return fallback
    profile = _shell_known_folder("Profile")
    if not profile or not _same_folder(str(home), profile):
        return fallback
    reported = _shell_known_folder(name)
    if not reported or not _usable(reported, profile):
        return fallback
    try:
        resolved = _resolved(reported)
        real_profile = _resolved(profile)
    except (OSError, ValueError):
        return fallback
    if not _usable(resolved, profile) or _too_wide(resolved, real_profile):
        return fallback
    return Path(resolved)


def program_files_folders() -> list[str]:
    """The Program Files folders the shell reports (64-bit, then 32-bit), each once. A folder on a share,
    a device or a network drive is never reported; off Windows there are none."""
    if not _IS_WINDOWS:
        return []
    folders: list[str] = []
    for name in ("ProgramFiles", "ProgramFilesX86"):
        reported = _shell_known_folder(name)
        if not reported or not _full_local_path(reported):
            continue
        if _drive_is_remote(ntpath.splitdrive(reported)[0]) is not False:
            continue
        if not any(_same_folder(reported, seen) for seen in folders):
            folders.append(reported)
    return folders


def on_local_drive(value: str) -> bool:
    """Whether `value` is a full path on a drive of this machine, so resolving it opens nothing remote:
    judged on its spelling first (no share, device or partial form), then the drive's type. A drive
    Windows cannot type counts as remote."""
    if not _full_local_path(value):
        return False
    return _drive_is_remote(ntpath.splitdrive(value)[0]) is False


def known_folder(name: str) -> str | None:
    """The folder the shell reports for `name` (one of `_FOLDER_IDS`) as a full local path, or None
    off Windows or when the shell cannot answer. Redirected folders (OneDrive backup, known-folder
    move) are reported where they really are, which an environment variable would miss. Why a
    None came back is kept for `outcome`: an absent folder and a failed lookup are not the same."""
    clear_outcome(name)
    if not _IS_WINDOWS or name not in _FOLDER_IDS:
        return None
    reported = _shell_known_folder(name)
    if not reported:
        if outcome(name) != ABSENT and not outcome(name).startswith(FAILED):
            # No answer and no word on why: a failed lookup (fail closed), never an absent folder.
            _record(name, f"{FAILED}: no answer")
        return None
    if not _full_local_path(reported):
        _record(name, f"{FAILED}: not a full local path")
        return None
    _record(name, FOUND)
    return reported
