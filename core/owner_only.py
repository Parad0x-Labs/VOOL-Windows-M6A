"""Owner-only files and folders, on POSIX and Windows alike.

POSIX keeps today's modes: ``0o600`` for a file, ``0o700`` for a folder. On Windows those modes only
toggle the read-only bit, so a "private" file keeps the parent folder's inherited ACL, which under a
non-profile root (``C:\\VOOL``, a second drive) often lets ``BUILTIN\\Users`` read it. There the file
gets a protected DACL with one ACE, full access for the current user, and the user as its owner:

- file:   ``O:<user>D:P(A;;FA;;;<user>)``
- folder: ``O:<user>D:P(A;OICI;FA;;;<user>)`` -- files and folders created inside inherit owner-only
  at creation, so a file written into a private folder is never readable by anyone else, not even
  for the moment between its creation and a later ``make_owner_only``.

``P`` (SE_DACL_PROTECTED) drops every ACE inherited from the parent instead of adding to it. The
owner is set explicitly: an elevated process creates files owned by ``BUILTIN\\Administrators``.
SYSTEM and Administrators get no ACE, matching ``0o600``.

Everything is done by handle, never by path (docs/WINDOWS_BROKER.md: never ``SetNamedSecurityInfo``):
the file is opened with FILE_FLAG_OPEN_REPARSE_POINT, a reparse point (symlink, junction) is refused
on that handle, and the descriptor is set and then read back on the same handle. A volume without
persistent ACLs (FAT, exFAT, some shares) is refused. Nothing here follows a link: securing a link's
target would let whoever placed the link pick what gets re-permissioned.

``make_owner_only`` and ``verify_owner_only`` return an :class:`OwnerOnlyReport` and never raise for
an expected failure; ``require_owner_only`` and ``ensure_private_dir`` raise
:class:`OwnerOnlyUnavailable`, for secrets that must not be written unprotected.
Design: jobs/broker/B3-owner-only-acl/DESIGN.md.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WINDOWS = sys.platform == "win32"

_FILE_SDDL = "O:{sid}D:P(A;;FA;;;{sid})"
_DIRECTORY_SDDL = "O:{sid}D:P(A;OICI;FA;;;{sid})"
_ACCESS_ALLOWED_ACE_TYPE = 0
#: Windows hands a path this long or longer to the long-path form (``\\?\``); MAX_PATH less room
#: for an 8.3 name, as CreateDirectoryW counts it.
_LONG_PATH = 248


@dataclass(frozen=True)
class OwnerOnlyReport:
    """Whether a path is owner-only; ``reason`` names the first thing that is not ("" when ok)."""

    ok: bool
    reason: str = ""


class OwnerOnlyUnavailable(OSError):
    """A path could not be made owner-only, so nothing secret may be written there."""

    def __init__(self, path: str | os.PathLike[str], reason: str) -> None:
        super().__init__(f"cannot make {os.fspath(path)!r} owner-only: {reason}")
        self.reason = reason


def make_owner_only(path: str | os.PathLike[str], *, directory: bool = False) -> OwnerOnlyReport:
    """Make ``path`` (an existing file, or folder when ``directory``) owner-only, then verify it."""
    if not _WINDOWS:
        return _posix_make(os.fspath(path), directory=directory)
    return _windows_apply(os.fspath(path), directory=directory, set_descriptor=True)


def verify_owner_only(path: str | os.PathLike[str]) -> OwnerOnlyReport:
    """Whether ``path`` is owner-only now. Reads only; never changes anything."""
    if not _WINDOWS:
        return _posix_verify(os.fspath(path))
    return _windows_apply(os.fspath(path), directory=None, set_descriptor=False)


def require_owner_only(path: str | os.PathLike[str], *, directory: bool = False) -> None:
    """:func:`make_owner_only`, raising :class:`OwnerOnlyUnavailable` when it is not reached."""
    report = make_owner_only(path, directory=directory)
    if not report.ok:
        raise OwnerOnlyUnavailable(path, report.reason)


def ensure_private_dir(path: str | os.PathLike[str]) -> Path:
    """Create ``path`` owner-only (its parents are created as ordinary folders), or make an existing
    one owner-only; raise :class:`OwnerOnlyUnavailable` when it cannot be."""
    folder = os.path.abspath(os.fspath(path))
    if not _WINDOWS:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        require_owner_only(folder, directory=True)
        return Path(folder)
    os.makedirs(os.path.dirname(folder), exist_ok=True)
    api = _win_api()
    created = api.create_directory(_win_long_path(folder), _DIRECTORY_SDDL.format(sid=api.user_sid()))
    report = (
        _windows_apply(folder, directory=True, set_descriptor=False)
        if created
        else make_owner_only(folder, directory=True)
    )
    if not report.ok:
        raise OwnerOnlyUnavailable(folder, report.reason)
    return Path(folder)


# --- POSIX ---------------------------------------------------------------------------------------------
def _posix_make(path: str, *, directory: bool) -> OwnerOnlyReport:
    try:
        info = os.lstat(path)
    except OSError:
        return OwnerOnlyReport(False, "unreadable")
    if stat.S_ISLNK(info.st_mode):
        return OwnerOnlyReport(False, "reparse_point")
    if stat.S_ISDIR(info.st_mode) is not directory:
        return OwnerOnlyReport(False, "kind_mismatch")
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        return OwnerOnlyReport(False, "set_failed")
    return _posix_verify(path)


def _posix_verify(path: str) -> OwnerOnlyReport:
    try:
        info = os.lstat(path)
    except OSError:
        return OwnerOnlyReport(False, "unreadable")
    if stat.S_ISLNK(info.st_mode):
        return OwnerOnlyReport(False, "reparse_point")
    if info.st_uid != os.geteuid():
        return OwnerOnlyReport(False, "owner_not_user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        return OwnerOnlyReport(False, "mode_too_open")
    return OwnerOnlyReport(True)


# --- Windows policy (the calls themselves are in _WinApi) -------------------------------------------
def _windows_apply(path: str, *, directory: bool | None, set_descriptor: bool) -> OwnerOnlyReport:
    """Open ``path`` by handle, refuse what must not be touched, optionally set the owner-only
    descriptor, then read it back on the same handle. ``directory`` None accepts either kind."""
    api = _win_api()
    try:
        handle = api.open(_win_long_path(os.path.abspath(path)))
    except OSError:
        return OwnerOnlyReport(False, "unreadable")
    try:
        if api.is_reparse_point(handle):
            return OwnerOnlyReport(False, "reparse_point")
        is_directory = api.is_directory(handle)
        if directory is not None and is_directory is not directory:
            return OwnerOnlyReport(False, "kind_mismatch")
        if not api.has_persistent_acls(handle):
            return OwnerOnlyReport(False, "no_persistent_acls")
        user = api.user_sid()
        if set_descriptor:
            template = _DIRECTORY_SDDL if is_directory else _FILE_SDDL
            try:
                api.set_descriptor(handle, template.format(sid=user))
            except OSError:
                return OwnerOnlyReport(False, "set_failed")
        try:
            owner, protected, dacl = api.read_descriptor(handle)
        except OSError:
            return OwnerOnlyReport(False, "unreadable")
        return _judge_descriptor(user, owner, protected, dacl)
    finally:
        api.close(handle)


def _judge_descriptor(
    user: str, owner: str, protected: bool, dacl: list[tuple[int, str]] | None
) -> OwnerOnlyReport:
    if dacl is None:
        return OwnerOnlyReport(False, "null_dacl")  # a NULL DACL grants everyone everything
    if owner != user:
        return OwnerOnlyReport(False, "owner_not_user")
    if not protected:
        return OwnerOnlyReport(False, "dacl_not_protected")
    if not dacl:
        return OwnerOnlyReport(False, "empty_dacl")  # locks the user out too: not what was asked
    if any(ace_type != _ACCESS_ALLOWED_ACE_TYPE or sid != user for ace_type, sid in dacl):
        return OwnerOnlyReport(False, "foreign_ace")
    return OwnerOnlyReport(True)


def _win_long_path(path: str) -> str:
    """``path`` in the ``\\\\?\\`` form when it is too long for the plain one."""
    if len(path) < _LONG_PATH or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


_API: Any = None


def _win_api() -> Any:
    global _API
    if _API is None:
        _API = _WinApi()
    return _API


class _WinApi:
    """The advapi32/kernel32 calls, over ctypes. Built only on Windows."""

    _GENERIC_ACCESS_WITH_OWNER = 0x00020000 | 0x00040000 | 0x00080000  # READ_CONTROL|WRITE_DAC|WRITE_OWNER
    _GENERIC_ACCESS = 0x00020000 | 0x00040000  # READ_CONTROL|WRITE_DAC
    _SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FLAGS = 0x00200000 | 0x02000000  # FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_BACKUP_SEMANTICS
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_PERSISTENT_ACLS = 0x8
    _SE_FILE_OBJECT = 1
    _OWNER = 0x1
    _DACL = 0x4
    _PROTECTED_DACL = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _ERROR_ACCESS_DENIED = 5
    _ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        k, a = self._kernel32, self._advapi32
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.GetVolumeInformationByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        k.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        k.LocalFree.argtypes = [ctypes.c_void_p]
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ]
        a.GetSecurityDescriptorOwner.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
        ]
        a.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        a.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
        ]
        a.SetSecurityInfo.restype = wintypes.DWORD
        a.SetSecurityInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        a.GetSecurityInfo.restype = wintypes.DWORD
        a.GetSecurityInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        a.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        self._user_sid: str | None = None
        self._can_set_owner: dict[int, bool] = {}

    def _error(self, what: str) -> OSError:
        code = self._ctypes.get_last_error()
        return self._ctypes.WinError(code, f"{what} failed")

    def _sid_string(self, psid: Any) -> str:
        text = self._wintypes.LPWSTR()
        if not self._advapi32.ConvertSidToStringSidW(psid, self._ctypes.byref(text)):
            raise self._error("ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            self._kernel32.LocalFree(text)

    def user_sid(self) -> str:
        if self._user_sid is not None:
            return self._user_sid
        ctypes, wintypes = self._ctypes, self._wintypes
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise self._error("OpenProcessToken")
        try:
            size = wintypes.DWORD()
            self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not self._advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise self._error("GetTokenInformation")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
            psid = ctypes.c_void_p.from_buffer(buffer).value
            self._user_sid = self._sid_string(psid)
        finally:
            self._kernel32.CloseHandle(token)
        return self._user_sid

    def open(self, path: str) -> int:
        k = self._kernel32
        handle = k.CreateFileW(
            path, self._GENERIC_ACCESS_WITH_OWNER, self._SHARE_ALL, None, self._OPEN_EXISTING, self._FLAGS, None
        )
        can_set_owner = True
        if handle in (None, -1, self._wintypes.HANDLE(-1).value):
            if self._ctypes.get_last_error() != self._ERROR_ACCESS_DENIED:
                raise self._error("CreateFileW")
            # No WRITE_OWNER: set the DACL only. The read-back still requires the user as owner.
            handle = k.CreateFileW(
                path, self._GENERIC_ACCESS, self._SHARE_ALL, None, self._OPEN_EXISTING, self._FLAGS, None
            )
            if handle in (None, -1, self._wintypes.HANDLE(-1).value):
                raise self._error("CreateFileW")
            can_set_owner = False
        self._can_set_owner[int(handle)] = can_set_owner
        return int(handle)

    def close(self, handle: int) -> None:
        self._can_set_owner.pop(handle, None)
        self._kernel32.CloseHandle(handle)

    def _attributes(self, handle: int) -> int:
        info = (self._wintypes.DWORD * 2)()  # FILE_ATTRIBUTE_TAG_INFO: FileAttributes, ReparseTag
        if not self._kernel32.GetFileInformationByHandleEx(
            handle, self._FILE_ATTRIBUTE_TAG_INFO, info, self._ctypes.sizeof(info)
        ):
            raise self._error("GetFileInformationByHandleEx")
        return int(info[0])

    def is_reparse_point(self, handle: int) -> bool:
        return bool(self._attributes(handle) & self._FILE_ATTRIBUTE_REPARSE_POINT)

    def is_directory(self, handle: int) -> bool:
        return bool(self._attributes(handle) & self._FILE_ATTRIBUTE_DIRECTORY)

    def has_persistent_acls(self, handle: int) -> bool:
        flags = self._wintypes.DWORD()
        if not self._kernel32.GetVolumeInformationByHandleW(
            handle, None, 0, None, None, self._ctypes.byref(flags), None, 0
        ):
            return False
        return bool(flags.value & self._FILE_PERSISTENT_ACLS)

    def set_descriptor(self, handle: int, sddl: str) -> None:
        ctypes, wintypes = self._ctypes, self._wintypes
        sd = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise self._error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            if not self._advapi32.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(defaulted)):
                raise self._error("GetSecurityDescriptorOwner")
            if not self._advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
                raise self._error("GetSecurityDescriptorDacl")
            which = self._DACL | self._PROTECTED_DACL
            if self._can_set_owner.get(handle, False):
                which |= self._OWNER
            status = self._advapi32.SetSecurityInfo(
                handle, self._SE_FILE_OBJECT, which, owner if which & self._OWNER else None, None, dacl, None
            )
            if status != 0:
                raise ctypes.WinError(status, "SetSecurityInfo failed")
        finally:
            self._kernel32.LocalFree(sd)

    def read_descriptor(self, handle: int) -> tuple[str, bool, list[tuple[int, str]] | None]:
        ctypes, wintypes = self._ctypes, self._wintypes
        owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self._advapi32.GetSecurityInfo(
            handle, self._SE_FILE_OBJECT, self._OWNER | self._DACL,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd),
        )
        if status != 0:
            raise ctypes.WinError(status, "GetSecurityInfo failed")
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if not self._advapi32.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
                raise self._error("GetSecurityDescriptorControl")
            protected = bool(control.value & self._SE_DACL_PROTECTED)
            owner_sid = self._sid_string(owner) if owner.value else ""
            if not dacl.value:
                return owner_sid, protected, None
            # ACL header: AclRevision (BYTE), Sbz1 (BYTE), AclSize (WORD), AceCount (WORD), Sbz2 (WORD).
            ace_count = ctypes.c_uint16.from_address(dacl.value + 4).value
            aces: list[tuple[int, str]] = []
            for index in range(ace_count):
                ace = ctypes.c_void_p()
                if not self._advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    raise self._error("GetAce")
                ace_type = ctypes.c_uint8.from_address(ace.value).value
                # ACE_HEADER (4 bytes) then Mask (DWORD); the SID starts at offset 8 for the
                # ACCESS_ALLOWED / ACCESS_DENIED shapes. Any other type is foreign anyway.
                sid = self._sid_string(ace.value + 8) if ace_type in (0, 1) else ""
                aces.append((ace_type, sid))
            return owner_sid, protected, aces
        finally:
            self._kernel32.LocalFree(sd)

    def create_directory(self, path: str, sddl: str) -> bool:
        ctypes, wintypes = self._ctypes, self._wintypes

        class _SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        sd = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise self._error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), sd, False)
            if self._kernel32.CreateDirectoryW(path, ctypes.byref(attributes)):
                return True
            if ctypes.get_last_error() == self._ERROR_ALREADY_EXISTS:
                return False
            raise self._error("CreateDirectoryW")
        finally:
            self._kernel32.LocalFree(sd)
