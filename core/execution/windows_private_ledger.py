"""Owner-only Windows mutation ledgers. Never truncate before native access control is verified."""
from __future__ import annotations

import ctypes as ct
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import Path


class _Attributes(ct.Structure):
    _fields_ = [("length", wt.DWORD), ("descriptor", ct.c_void_p), ("inherit", wt.BOOL)]


class _Acl(ct.Structure):
    _fields_ = [("revision", wt.BYTE), ("reserved", wt.BYTE), ("size", wt.WORD),
                ("count", wt.WORD), ("reserved2", wt.WORD)]


class _Win32:
    def __init__(self):
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.security = ct.WinDLL("advapi32", use_last_error=True)
        pointer = ct.c_void_p
        out = ct.POINTER(pointer)
        definitions = (
            (self.kernel, "GetCurrentProcess", pointer, []),
            (self.kernel, "CloseHandle", wt.BOOL, [pointer]),
            (self.kernel, "LocalFree", pointer, [pointer]),
            (self.kernel, "CreateFileW", pointer, [wt.LPCWSTR, wt.DWORD, wt.DWORD, pointer, wt.DWORD, wt.DWORD, pointer]),
            (self.kernel, "GetFileInformationByHandleEx", wt.BOOL, [pointer, ct.c_int, pointer, wt.DWORD]),
            (self.kernel, "SetEndOfFile", wt.BOOL, [pointer]),
            (self.kernel, "WriteFile", wt.BOOL, [pointer, pointer, wt.DWORD, ct.POINTER(wt.DWORD), pointer]),
            (self.kernel, "FlushFileBuffers", wt.BOOL, [pointer]),
            (self.security, "OpenProcessToken", wt.BOOL, [pointer, wt.DWORD, out]),
            (self.security, "GetTokenInformation", wt.BOOL, [pointer, ct.c_int, pointer, wt.DWORD, ct.POINTER(wt.DWORD)]),
            (self.security, "ConvertSidToStringSidW", wt.BOOL, [pointer, out]),
            (self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL, [wt.LPCWSTR, wt.DWORD, out, pointer]),
            (self.security, "GetSecurityDescriptorDacl", wt.BOOL, [pointer, ct.POINTER(wt.BOOL), out, ct.POINTER(wt.BOOL)]),
            (self.security, "GetSecurityDescriptorControl", wt.BOOL, [pointer, ct.POINTER(wt.WORD), ct.POINTER(wt.DWORD)]),
            (self.security, "GetSecurityInfo", wt.DWORD, [pointer, ct.c_int, wt.DWORD, out, pointer, out, pointer, out]),
            (self.security, "SetSecurityInfo", wt.DWORD, [pointer, ct.c_int, wt.DWORD, pointer, pointer, pointer, pointer]),
            (self.security, "GetAce", wt.BOOL, [pointer, wt.DWORD, out]),
            (self.security, "EqualSid", wt.BOOL, [pointer, pointer]),
        )
        for library, name, result, arguments in definitions:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments

    @staticmethod
    def require(ok):
        if not ok:
            raise ct.WinError(ct.get_last_error())

    @contextmanager
    def user(self):
        token = ct.c_void_p()
        self.require(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ct.byref(token)))
        try:
            needed = wt.DWORD()
            if self.security.GetTokenInformation(token, 1, None, 0, ct.byref(needed)) or ct.get_last_error() != 122:
                raise OSError("Could not size the Windows user identity")
            if not ct.sizeof(ct.c_void_p) <= needed.value <= 65536:
                raise OSError("Invalid Windows user identity size")
            buffer = ct.create_string_buffer(needed.value)
            self.require(self.security.GetTokenInformation(token, 1, buffer, len(buffer), ct.byref(needed)))
            sid = ct.c_void_p.from_buffer(buffer).value
            text = ct.c_void_p()
            self.require(self.security.ConvertSidToStringSidW(sid, ct.byref(text)))
            try:
                yield sid, ct.wstring_at(text)
            finally:
                self.kernel.LocalFree(text)
        finally:
            self.kernel.CloseHandle(token)

    @contextmanager
    def descriptor(self, user, directory):
        descriptor = ct.c_void_p()
        flags = "OICI" if directory else ""
        self.require(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{user}D:P(A;{flags};FA;;;{user})", 1, ct.byref(descriptor), None))
        try:
            present, defaulted, dacl = wt.BOOL(), wt.BOOL(), ct.c_void_p()
            self.require(self.security.GetSecurityDescriptorDacl(descriptor, ct.byref(present), ct.byref(dacl), ct.byref(defaulted)))
            if not present.value or not dacl.value:
                raise PermissionError("An owner-only DACL is required")
            yield descriptor, dacl
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def handle(self, path, descriptor, directory):
        attributes = _Attributes(ct.sizeof(_Attributes), descriptor, False)
        # No delete sharing: retain custody of the directory/file while permissions and bytes change.
        access = 0x00060000 | (0 if directory else 0x40000000)  # READ_CONTROL | WRITE_DAC | GENERIC_WRITE
        handle = self.kernel.CreateFileW(str(path), access, 3 if directory else 0, ct.byref(attributes),
                                         3 if directory else 4, 0x02200000, None)
        if handle == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        try:
            tag = (wt.DWORD * 2)()
            self.require(self.kernel.GetFileInformationByHandleEx(handle, 9, tag, ct.sizeof(tag)))
            if tag[0] & 0x400 or bool(tag[0] & 0x10) != directory:
                raise PermissionError("Mutation ledger reparse points or unexpected object types are refused")
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def restrict(self, handle, user_sid, dacl, directory):
        self.verify(handle, user_sid, directory, owner_only=False)
        error = self.security.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ct.WinError(error)
        self.verify(handle, user_sid, directory, owner_only=True)

    def verify(self, handle, user_sid, directory, *, owner_only):
        owner, dacl, descriptor = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
        error = self.security.GetSecurityInfo(handle, 1, 5, ct.byref(owner), None, ct.byref(dacl), None, ct.byref(descriptor))
        try:
            if error:
                raise ct.WinError(error)
            if not owner.value or not self.security.EqualSid(owner, user_sid):
                raise PermissionError("Mutation ledger ownership differs from the current Windows user")
            if not owner_only:
                return
            control, revision = wt.WORD(), wt.DWORD()
            self.require(self.security.GetSecurityDescriptorControl(descriptor, ct.byref(control), ct.byref(revision)))
            if not control.value & 0x1000 or not dacl.value or _Acl.from_address(dacl.value).count != 1:
                raise PermissionError("Mutation ledger must have one protected owner-only access entry")
            ace = ct.c_void_p()
            self.require(self.security.GetAce(dacl, 0, ct.byref(ace)))
            header = (wt.BYTE * 4).from_address(ace.value)
            if (header[0] != 0 or header[1] != (3 if directory else 0)
                    or wt.WORD.from_address(ace.value + 2).value < 12
                    or wt.DWORD.from_address(ace.value + 4).value != 0x001F01FF
                    or not self.security.EqualSid(ace.value + 8, user_sid)):
                raise PermissionError("Mutation ledger access is not exclusively owned by the current user")
        finally:
            if descriptor.value:
                self.kernel.LocalFree(descriptor)

    def write(self, handle, payload):
        # OPEN_ALWAYS above does not truncate. No secret bytes change before both ACL checks succeed.
        self.require(self.kernel.SetEndOfFile(handle))
        buffer = ct.create_string_buffer(payload)
        offset = 0
        while offset < len(payload):
            count, written = min(len(payload) - offset, 1024 * 1024), wt.DWORD()
            self.require(self.kernel.WriteFile(handle, ct.addressof(buffer) + offset, count, ct.byref(written), None))
            if not 0 < written.value <= count:
                raise OSError("Incomplete mutation ledger write")
            offset += written.value
        self.require(self.kernel.FlushFileBuffers(handle))


def write_owner_only_ledger(path: Path, payload: str) -> None:
    """Restrict the directory and file to this user before writing any rollback content."""
    api = _Win32()
    path.parent.mkdir(parents=True, exist_ok=True)
    with api.user() as (sid, user), api.descriptor(user, True) as (parent_sd, parent_dacl):
        with api.handle(path.parent, parent_sd, True) as parent:
            api.restrict(parent, sid, parent_dacl, True)
            with api.descriptor(user, False) as (file_sd, file_dacl):
                with api.handle(path, file_sd, False) as file:
                    api.restrict(file, sid, file_dacl, False)
                    api.write(file, payload.encode("utf-8"))
