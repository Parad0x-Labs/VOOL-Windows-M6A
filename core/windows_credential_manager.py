"""Windows Credential Manager as VOOL's key store, through advapi32 by ctypes (no third-party package).

Each credential is one generic credential (``CRED_TYPE_GENERIC``) in the signed-in user's Credential
Manager, persisted for this user on this computer (``CRED_PERSIST_LOCAL_MACHINE``: it survives sign-out
and restart and does not roam to other PCs). Windows seals it with DPAPI under the user's sign-in. Any
program running as this user can read it; nothing here claims more than that.

Target names are namespaced to VOOL, ``VOOL/<store>/<name>`` (``cmdkey /list:VOOL/*`` shows them), and
``UserName`` carries the credential name so the Credential Manager control panel shows what each is.

Encoding is explicit: target names, user names and comments go through the ``W`` (UTF-16) entry
points; the secret itself is stored as its UTF-8 bytes in ``CredentialBlob`` and read back as strict
UTF-8, so a blob VOOL did not write fails closed instead of being guessed at. A blob may hold at most
``CRED_MAX_CREDENTIAL_BLOB_SIZE`` (2560) bytes: a longer value is refused with
:class:`CredentialTooLarge` before anything is written. API keys are far smaller.

Errors never carry a secret: messages name the credential and the Windows error code only.

:class:`WindowsCredentialManager` has the small keyring-shaped surface ``core.credential_store``
already drives (``set_password`` / ``get_password`` / ``delete_password``) plus ``list_names``
(``CredEnumerateW``). :func:`backend` returns one when advapi32 loads and answers, else None.
Tests replace :func:`_load_advapi32` with a fake that speaks the same five calls.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any

#: The Windows store name VOOL's own credential slots live under: ``VOOL/credentials/<name>``.
TARGET_ROOT = "VOOL"
CREDENTIALS_STORE = "credentials"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512  # 2560 bytes (wincred.h)
CRED_MAX_GENERIC_TARGET_NAME_LENGTH = 32767

ERROR_NOT_FOUND = 1168
ERROR_NO_SUCH_LOGON_SESSION = 1312

DWORD = ctypes.c_uint32  # explicit width: ctypes.wintypes.DWORD is c_ulong, 8 bytes off Windows


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class CREDENTIALW(ctypes.Structure):
    """wincred.h ``CREDENTIALW``."""

    _fields_ = [
        ("Flags", DWORD),
        ("Type", DWORD),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", DWORD),
        ("AttributeCount", DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


class CredentialManagerError(OSError):
    """A Credential Manager call failed with a Windows error code. Never carries a secret."""

    def __init__(self, what: str, winerror: int) -> None:
        super().__init__(f"Windows Credential Manager {what} failed (Windows error {winerror})")
        self.winerror = winerror


class CredentialNotFound(CredentialManagerError):
    """``ERROR_NOT_FOUND``: there is no such VOOL credential (a delete of an absent name)."""


class CredentialTooLarge(ValueError):
    """The value is longer than a Credential Manager blob can hold; nothing was written."""

    def __init__(self, name: str, size: int) -> None:
        super().__init__(
            f"The value for {name} is {size} bytes, longer than the {CRED_MAX_CREDENTIAL_BLOB_SIZE} bytes "
            "Windows Credential Manager can hold, so it was not saved."
        )
        self.size = size


class _Advapi32:
    """The five advapi32 calls, typed. Only constructed on Windows."""

    def __init__(self) -> None:
        dll = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        self.CredWriteW = dll.CredWriteW
        self.CredWriteW.argtypes = [PCREDENTIALW, DWORD]
        self.CredWriteW.restype = ctypes.c_int
        self.CredReadW = dll.CredReadW
        self.CredReadW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD, ctypes.POINTER(PCREDENTIALW)]
        self.CredReadW.restype = ctypes.c_int
        self.CredDeleteW = dll.CredDeleteW
        self.CredDeleteW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD]
        self.CredDeleteW.restype = ctypes.c_int
        self.CredEnumerateW = dll.CredEnumerateW
        self.CredEnumerateW.argtypes = [
            ctypes.c_wchar_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(ctypes.POINTER(PCREDENTIALW))
        ]
        self.CredEnumerateW.restype = ctypes.c_int
        self.CredFree = dll.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]
        self.CredFree.restype = None

    @staticmethod
    def last_error() -> int:
        return int(ctypes.get_last_error())  # type: ignore[attr-defined]


def _load_advapi32() -> Any:
    """The advapi32 call surface, or None off Windows or when it does not load."""
    if sys.platform != "win32":
        return None
    try:
        return _Advapi32()
    except Exception:
        return None


def _check_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("credential name is required")
    if any(ch in text for ch in "*\\/") or any(ord(ch) < 0x20 for ch in text):
        # '*' is CredEnumerateW's wildcard; '/' separates the namespace parts.
        raise ValueError("credential name may not contain '*', '/', '\\' or control characters")
    return text


class WindowsCredentialManager:
    """VOOL's credentials in Windows Credential Manager, under ``VOOL/<store>/``.

    ``service`` is the store id ``core.credential_store`` passes on every call; it must be the one
    this instance was made for, so a call can never land in another namespace by mistake.
    """

    #: Credential Manager never shows a dialog for a generic credential: no operator grant is needed
    #: for a write, unlike the macOS Keychain (core.keychain_policy).
    non_interactive = True
    display_name = "Windows Credential Manager"

    def __init__(self, api: Any, *, service: str, store: str = CREDENTIALS_STORE) -> None:
        self._api = api
        self._service = service
        self._prefix = f"{TARGET_ROOT}/{store}/"

    # ------------------------------------------------------------------ helpers

    def _target(self, service: str, name: str) -> str:
        if service != self._service:
            raise ValueError("credential service does not match this Credential Manager store")
        target = self._prefix + _check_name(name)
        if len(target) > CRED_MAX_GENERIC_TARGET_NAME_LENGTH:
            raise ValueError("credential name is too long for Windows Credential Manager")
        return target

    def target_name(self, name: str) -> str:
        """The Credential Manager target a credential name is stored under (no secret)."""
        return self._target(self._service, name)

    # ------------------------------------------------------------------ keyring-shaped surface

    def set_password(self, service: str, name: str, value: str) -> None:
        target = self._target(service, name)
        blob = str(value).encode("utf-8")
        if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialTooLarge(_check_name(name), len(blob))
        buffer = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "VOOL API key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = _check_name(name)
        try:
            if not self._api.CredWriteW(ctypes.byref(cred), 0):
                raise CredentialManagerError(f"write of {_check_name(name)}", self._api.last_error())
        finally:
            ctypes.memset(buffer, 0, ctypes.sizeof(buffer))  # drop our copy of the secret bytes

    def get_password(self, service: str, name: str) -> str | None:
        target = self._target(service, name)
        pcred = PCREDENTIALW()
        if not self._api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            code = self._api.last_error()
            if code == ERROR_NOT_FOUND:
                return None
            raise CredentialManagerError(f"read of {_check_name(name)}", code)
        try:
            cred = pcred.contents
            size = int(cred.CredentialBlobSize)
            if size > CRED_MAX_CREDENTIAL_BLOB_SIZE:
                raise CredentialManagerError(f"read of {_check_name(name)} (blob too large)", 0)
            raw = ctypes.string_at(cred.CredentialBlob, size) if size else b""
        finally:
            self._api.CredFree(pcred)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # Not a blob VOOL wrote (UTF-8): an unknown state, never a guessed value.
            raise CredentialManagerError(f"read of {_check_name(name)} (blob is not UTF-8)", 0) from None

    def delete_password(self, service: str, name: str) -> None:
        target = self._target(service, name)
        if not self._api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            code = self._api.last_error()
            if code == ERROR_NOT_FOUND:
                raise CredentialNotFound(f"delete of {_check_name(name)}", code)
            raise CredentialManagerError(f"delete of {_check_name(name)}", code)

    def list_names(self, service: str) -> list[str]:
        """Every credential name stored under this store's namespace (no values are read)."""
        if service != self._service:
            raise ValueError("credential service does not match this Credential Manager store")
        count = DWORD(0)
        pcreds = ctypes.POINTER(PCREDENTIALW)()
        if not self._api.CredEnumerateW(self._prefix + "*", 0, ctypes.byref(count), ctypes.byref(pcreds)):
            code = self._api.last_error()
            if code == ERROR_NOT_FOUND:
                return []
            raise CredentialManagerError("enumerate", code)
        names: list[str] = []
        try:
            for index in range(int(count.value)):
                cred = pcreds[index].contents
                target = str(cred.TargetName or "")
                if int(cred.Type) != CRED_TYPE_GENERIC or not target.startswith(self._prefix):
                    continue
                name = target[len(self._prefix):]
                try:
                    names.append(_check_name(name))
                except ValueError:
                    continue  # not a name VOOL could have written
        finally:
            self._api.CredFree(pcreds)
        return sorted(set(names))


_PROBED: dict[str, Any] = {}
_PROBE_LOCK = threading.Lock()


def backend(service: str) -> WindowsCredentialManager | None:
    """A working Credential Manager store for ``service``, or None when it cannot be used here.

    Probed once per process with an enumerate of VOOL's own namespace (reads no value): a session
    with no logon session (some service or scheduled-task contexts) answers
    ``ERROR_NO_SUCH_LOGON_SESSION`` and is reported unavailable, so the caller keeps the vault."""
    with _PROBE_LOCK:
        if service in _PROBED:
            return _PROBED[service]
        api = _load_advapi32()
        store = None
        if api is not None:
            candidate = WindowsCredentialManager(api, service=service)
            try:
                candidate.list_names(service)
                store = candidate
            except Exception:
                store = None
        _PROBED[service] = store
        return store


def reset_probe() -> None:
    """Forget the per-process probe result (tests)."""
    with _PROBE_LOCK:
        _PROBED.clear()


__all__ = [
    "CRED_MAX_CREDENTIAL_BLOB_SIZE",
    "CRED_PERSIST_LOCAL_MACHINE",
    "CRED_TYPE_GENERIC",
    "CredentialManagerError",
    "CredentialNotFound",
    "CredentialTooLarge",
    "WindowsCredentialManager",
    "backend",
    "reset_probe",
]
