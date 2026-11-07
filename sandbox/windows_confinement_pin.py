"""The pinned VoolConfine launcher folder (docs/WINDOWS_CONFINEMENT.md section 2).

windows/broker/publish.ps1 publishes VoolConfine self-contained into its own folder and writes a pin
manifest that names EVERY file in it with its SHA-256:

    installed: {app}\\broker\\confine\\          pin {app}\\config\\confine-pin.json
    checkout:  windows\\broker\\publish\\confine\\   pin windows\\broker\\publish\\confine-pin.json

`verified_launcher` hands out VoolConfine.exe only when the folder holds exactly the pinned files,
each with its pinned hash. Hashing the exe alone would pin nothing: the launcher's code is in
VoolConfine.dll and VoolConfine.Core.dll, the runtime is in coreclr.dll and friends beside it, and
Windows loads a DLL planted beside an exe before the system copy, so an unlisted file is refused
too. A missing pin, a malformed pin, a missing file, an extra file, a link or reparse point, or a
hash mismatch all mean None: the backend is unavailable and the caller refuses the job.

On Windows every verified file stays open in this process with FILE_SHARE_READ only, so nobody can
write, replace, rename or delete it while VOOL runs, and later calls only re-list the folder and
check each path still names the file held open (no re-hash of ~70 MB per job). Elsewhere (the Linux
tests) nothing can be held that way, so every call verifies from scratch.

What the pin does NOT defend against: the program folder is per-user and writable by the user, and
the pin sits in that same folder, so a process running as the user can rewrite VOOL itself, pin
included. The pin stops a missing, partial or swapped launcher, a planted DLL, and a corrupted
copy; the confined child cannot reach the program folder at all (it is outside every write root).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import threading
from pathlib import Path

PIN_SCHEMA = "vool.windows_confinement.pin.v1"
LAUNCHER_NAME = "VoolConfine.exe"
FOLDER_NAME = "confine"
PIN_NAME = "confine-pin.json"

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_HEX = frozenset("0123456789abcdef")
_MAX_PIN_BYTES = 1 << 20


def launcher_locations(project_root: Path) -> tuple[tuple[Path, Path], ...]:
    """(confine folder, pin file) pairs, installed first, in the same order the broker executable is found.

    `project_root` is the app root: `{app}\\app` when installed, the repository root in a checkout."""
    installed = project_root.parent
    checkout = project_root / "windows" / "broker" / "publish"
    return (
        (installed / "broker" / FOLDER_NAME, installed / "config" / PIN_NAME),
        (checkout / FOLDER_NAME, checkout / PIN_NAME),
    )


def _is_link_or_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _valid_relpath(key: object) -> bool:
    """A pin key: a relative path with '/' separators that can only name one file on Windows."""
    if not isinstance(key, str) or not key or key.startswith("/") or key.endswith("/"):
        return False
    if any(ord(c) < 32 or c in '\\:*?"<>|' for c in key):
        return False
    # Windows drops trailing dots and spaces from a name, so "a.dll." would alias "a.dll".
    return all(part not in ("", ".", "..") and part[-1] not in ". " for part in key.split("/"))


def read_pin(path: Path) -> dict[str, str] | None:
    """The pin's {relpath: sha256}, or None when it is missing, a link, or breaks any rule."""
    try:
        st = os.lstat(path)
        if _is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_PIN_BYTES:
            return None
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"schema", "files"} or data["schema"] != PIN_SCHEMA:
        return None
    files = data["files"]
    if not isinstance(files, dict) or LAUNCHER_NAME not in files:
        return None
    folded: set[str] = set()
    for key, digest in files.items():
        if not _valid_relpath(key) or not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX:
            return None
        # Windows names are case-insensitive: two keys for one file would be an ambiguous pin.
        if key.casefold() in folded:
            return None
        folded.add(key.casefold())
    return dict(files)


def _list_folder(folder: Path) -> dict[str, Path] | None:
    """Every file under `folder` as {relpath: path}, or None if anything there is not a plain
    file or directory (a link, junction or other reparse point is refused, never followed)."""
    found: dict[str, Path] = {}

    def walk(directory: Path, prefix: str) -> bool:
        try:
            with os.scandir(directory) as entries:
                listed = list(entries)
        except OSError:
            return False
        for entry in listed:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                return False
            if _is_link_or_reparse(st):
                return False
            relpath = prefix + entry.name
            if stat.S_ISDIR(st.st_mode):
                if not walk(Path(entry.path), relpath + "/"):
                    return False
            elif stat.S_ISREG(st.st_mode):
                found[relpath] = Path(entry.path)
            else:
                return False
        return True

    try:
        st = os.lstat(folder)
    except OSError:
        return None
    if _is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
        return None
    return found if walk(folder, "") else None


def _open_denying_writes(path: Path) -> int:
    """A read-only fd for `path`. On Windows the file is opened with FILE_SHARE_READ only (no write,
    no delete, so no rename either) and without following a reparse point."""
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    generic_read, file_share_read, open_existing = 0x80000000, 0x1, 3
    flag_open_reparse_point, flag_sequential_scan = 0x00200000, 0x08000000
    handle = create(str(path), generic_read, file_share_read, None, open_existing, flag_open_reparse_point | flag_sequential_scan, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise


def _sha256(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


class _Held:
    """The pinned files of one folder, held open (Windows) and identified by device and file id."""

    def __init__(self, folder: Path, pin: dict[str, str], fds: dict[str, tuple[int, int, int]]) -> None:
        self.folder = folder
        self.pin = pin
        self.fds = fds  # relpath -> (fd, st_dev, st_ino)

    def close(self) -> None:
        for fd, _, _ in self.fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self.fds = {}


_lock = threading.Lock()
_held: dict[str, _Held] = {}


def _verify(folder: Path, pin: dict[str, str], listed: dict[str, Path], hold: bool) -> _Held | None:
    fds: dict[str, tuple[int, int, int]] = {}
    ok = False
    try:
        for relpath, path in listed.items():
            fd = _open_denying_writes(path)
            fds[relpath] = (fd, 0, 0)
            st = os.fstat(fd)
            if _is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode) or _sha256(fd) != pin[relpath]:
                return None
            # Identify the held file the way later calls will (os.stat of its path), so both sides
            # use one notion of file id. Once opened without delete sharing, the path can't be
            # pointed at another file, so this stat names the file just hashed.
            by_path = os.stat(path, follow_symlinks=False)
            fds[relpath] = (fd, by_path.st_dev, by_path.st_ino)
        ok = True
        return _Held(folder, pin, fds)
    except OSError:
        return None
    finally:
        if not ok or not hold:
            for fd, _, _ in fds.values():
                with contextlib.suppress(OSError):
                    os.close(fd)


def _still_the_held_files(held: _Held, pin: dict[str, str], listed: dict[str, Path]) -> bool:
    if held.pin != pin or set(held.fds) != set(listed):
        return False
    for relpath, path in listed.items():
        _, dev, ino = held.fds[relpath]
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        if (st.st_dev, st.st_ino) != (dev, ino):
            return False
    return True


def verified_launcher(project_root: Path) -> Path | None:
    """VoolConfine.exe from the installed `{app}\\broker\\confine\\` or a checkout's
    `windows\\broker\\publish\\confine\\`, only when every file in that folder matches the pin
    manifest and no unlisted file is present. Never from PATH.

    The first location whose VoolConfine.exe exists is the only one checked: a tampered install
    never falls back to some other copy."""
    found = next(
        ((folder, pin_path) for folder, pin_path in launcher_locations(project_root)
         if os.path.lexists(folder / LAUNCHER_NAME)),
        None,
    )
    if found is None:
        return None
    folder, pin_path = found
    key = os.path.normcase(os.path.abspath(folder))
    hold = os.name == "nt"
    with _lock:
        held = _held.pop(key, None)
        verified = _check(folder, pin_path, held, hold)
        if held is not None and verified is not held:
            held.close()
        if verified is None:
            return None
        if hold:
            _held[key] = verified
    return folder / LAUNCHER_NAME


def _check(folder: Path, pin_path: Path, held: _Held | None, hold: bool) -> _Held | None:
    """The verified folder: `held` itself when it still names the same files, else a fresh
    verification. None on any miss (the caller then closes whatever it held)."""
    pin = read_pin(pin_path)
    listed = _list_folder(folder)
    if pin is None or listed is None:
        return None
    # Exactly the pinned names, compared as Windows compares them.
    if sorted(k.casefold() for k in listed) != sorted(k.casefold() for k in pin):
        return None
    by_fold = {k.casefold(): k for k in pin}
    listed = {by_fold[k.casefold()]: p for k, p in listed.items()}
    if held is not None and hold and _still_the_held_files(held, pin, listed):
        return held
    if held is not None:
        # Close the old handles first, so a re-verify of the same files is not refused by our own lock.
        held.close()
    return _verify(folder, pin, listed, hold)


def release_all() -> None:
    """Close every held launcher file (tests, and before VOOL replaces its own program files)."""
    with _lock:
        for held in _held.values():
            held.close()
        _held.clear()
