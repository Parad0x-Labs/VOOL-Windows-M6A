"""Where the Skill Codex signing authority keeps its keys and state on Windows (owner decisions D16, D17).

The Mac signer keeps everything in one 0700 folder and locks it with flock. Windows has neither mode
bits that mean anything nor flock, so here:

- The four secrets live in this user's Windows Credential Manager, one generic credential each under
  ``VOOL/registry-authority/<name>`` (persisted on this computer only, never roaming). The main (root)
  key is held only as passphrase-encrypted PKCS8; the online keys and the authenticator secret as hex.
  A missing or unreadable key refuses: there is no vault fallback and nothing makes a key except setup.
  VOOL's own credential surfaces are bound to ``VOOL/credentials/`` and never see this namespace.
- The state (spent list number, revoked skills, last authenticator code, settings, the trust root,
  the kept skill bytes and the check scratch) lives in exactly one owner-only folder,
  :data:`AUTHORITY_FOLDER` under the shell's LocalAppData. It is the single folder outside the
  disposable workspaces the authority writes to; any other folder is refused, as is one reached
  through a junction or a symlink.
- One signer at a time holds the folder's ``lock`` file (``core/os_file_lock``).

The authority is a command-line tool: nothing in the app reaches this module.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from core.registry.codec import RegistryError, require

#: The authority's one state folder, under the shell's LocalAppData (owner decision D17). Read only here.
AUTHORITY_FOLDER = ("VOOL", "registry-authority")
#: The Credential Manager store: targets are ``VOOL/registry-authority/<name>`` (owner decision D16).
STORE = "registry-authority"
#: The file names the Mac signer uses for its secrets, and the credential each one is on Windows.
KEYS = {"admission.key": "admission", "review.key": "review", "totp.key": "totp", "root.pem": "root"}
_TEXT_KEYS = frozenset({"root"})  # stored as the encrypted PEM text; the rest as hex
_REPARSE_POINT = 0x400

UNAVAILABLE = "Windows Credential Manager isn't available here, so the signing authority can't open its keys"


# ---- the one folder ----

def authority_folder() -> Path:
    """The folder the authority must use, from the shell's LocalAppData (never from the environment)."""
    from core import windows_known_folders

    base = windows_known_folders.known_folder("LocalAppData")
    require(bool(base), "Windows didn't say where this account's local app data is, so the authority stays closed")
    return Path(str(base), *AUTHORITY_FOLDER)


def _same(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _no_link_on_the_way(folder: Path) -> None:
    """No part of the folder's path is a junction, symlink or other reparse point."""
    current = Path(folder)
    for part in (current, *current.parents):
        try:
            attributes = getattr(os.lstat(part), "st_file_attributes", 0)
        except FileNotFoundError:
            continue  # not made yet (setup), so nothing there can redirect it
        require(not (attributes & _REPARSE_POINT) and not part.is_symlink(),
                "the authority folder's path goes through a link; it must be a plain folder")


def require_fixed_folder(directory: Path) -> Path:
    """``directory`` is exactly :func:`authority_folder`, spelled and resolved, with no link on the way."""
    expected = authority_folder()
    folder = Path(directory)
    require(_same(str(folder), str(expected)), "on Windows the authority folder must be " + str(expected))
    _no_link_on_the_way(folder)
    if folder.exists():
        require(_same(os.path.realpath(folder), str(expected)), "the authority folder's path goes through a link")
    return folder


def require_private(path: Path) -> None:
    """Only this user can reach ``path``, judged by its ACL (a reparse point is refused there too)."""
    from core import owner_only

    report = owner_only.verify_owner_only(path)
    require(report.ok, path.name + " is missing or readable by others")


def open_folder(directory: Path) -> Path:
    folder = require_fixed_folder(directory)
    require(folder.is_dir(), "the authority folder is missing; run setup first")
    require_private(folder)
    return folder


def make_folder(directory: Path) -> Path:
    """Create the folder new and owner-only; an existing one is refused, since keys are made once."""
    from core import owner_only

    folder = require_fixed_folder(directory)
    try:
        return owner_only.ensure_private_dir(folder, fresh=True)
    except owner_only.OwnerOnlyUnavailable as exc:
        if exc.reason == "already_exists":
            raise RegistryError("the authority folder already exists; keys are made once") from None
        raise RegistryError("the authority folder could not be made private (" + exc.reason + ")") from None


def read(path: Path) -> bytes:
    require(path.is_file(), path.name + " is missing or readable by others")
    require_private(path)
    return path.read_bytes()


def write(path: Path, data: bytes) -> None:
    from core import owner_only

    try:
        owner_only.write_private_file(path, data)
    except owner_only.OwnerOnlyUnavailable as exc:
        raise RegistryError(path.name + " could not be made private (" + exc.reason + "), so it wasn't saved") from None


@contextlib.contextmanager
def locked(folder: Path):
    from core import os_file_lock

    lock = folder / "lock"
    if not lock.exists():
        write(lock, b"")
    require_private(lock)
    descriptor = os.open(lock, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os_file_lock.lock_exclusive(descriptor)
        yield
    finally:
        os.close(descriptor)  # closing releases the lock


# ---- the keys ----

def _store():
    from core import windows_credential_manager as wcm

    api = wcm._load_advapi32()
    require(api is not None, UNAVAILABLE)
    return wcm.WindowsCredentialManager(api, service=STORE, store=STORE, comment="VOOL Skill Codex signing key")


def _names(store) -> list[str]:
    from core import windows_credential_manager as wcm

    try:
        return store.list_names(STORE)
    except wcm.CredentialManagerError:
        raise RegistryError(UNAVAILABLE) from None


def read_key(file_name: str) -> bytes:
    """One secret, by the Mac signer's file name. Missing, unreadable or malformed refuses."""
    from core import windows_credential_manager as wcm

    name = KEYS[file_name]
    try:
        value = _store().get_password(STORE, name)
    except wcm.CredentialManagerError:
        raise RegistryError("the " + name + " key could not be read from Windows Credential Manager") from None
    require(value is not None, "the " + name + " key is missing from Windows Credential Manager")
    if name in _TEXT_KEYS:
        return value.encode("utf-8")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise RegistryError("the " + name + " key in Windows Credential Manager is not one VOOL wrote") from None


def write_keys(keys: dict[str, bytes]) -> None:
    """Store every secret once. Refused when any authority entry exists; a partial write is taken back."""
    from core import windows_credential_manager as wcm

    store = _store()
    require(not _names(store), "Windows Credential Manager already holds signing keys under VOOL/" + STORE +
            "; keys are made once")
    written: list[str] = []
    try:
        for file_name, data in keys.items():
            name = KEYS[file_name]
            store.set_password(STORE, name, data.decode("utf-8") if name in _TEXT_KEYS else data.hex())
            written.append(name)
    except (wcm.CredentialManagerError, wcm.CredentialTooLarge):
        for name in written:
            with contextlib.suppress(wcm.CredentialManagerError):
                store.delete_password(STORE, name)
        raise RegistryError("the signing keys could not be saved in Windows Credential Manager") from None


def forget_root() -> str:
    """Delete the main key from Credential Manager once the owner has it offline; daily signing never needs it."""
    from core import windows_credential_manager as wcm

    try:
        _store().delete_password(STORE, KEYS["root.pem"])
    except wcm.CredentialNotFound:
        return "This PC holds no main key."
    except wcm.CredentialManagerError:
        raise RegistryError("the main key could not be removed from Windows Credential Manager") from None
    return "Done. This PC no longer holds the main key; keep your offline copy and its passphrase safe."


__all__ = ["AUTHORITY_FOLDER", "KEYS", "STORE", "authority_folder", "forget_root", "locked", "make_folder", "open_folder", "read",
           "read_key", "require_fixed_folder", "require_private", "write", "write_keys"]
