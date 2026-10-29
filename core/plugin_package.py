"""Complete, bounded package identity and private execution copies.

There are no filename exclusions: executable code, vendored dependencies, locks,
references and empty directories all belong to the approved distribution. Runtime
state belongs in scratch. Links and unstable reads cannot produce an attestation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DIGEST_SCHEMA = "vool.plugin_package.v2"
APPROVAL_SCHEMA = "vool.plugin_approval.v1"
MAX_FILES = 4096
MAX_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
_WINDOWS = os.name == "nt"
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: symlinks, junctions, mount points


class PackageError(RuntimeError):
    """The complete package cannot be identified safely."""


@dataclass(frozen=True)
class PackageIdentity:
    digest: str
    manifest: dict
    entries: tuple[dict, ...]


def approval_digest(package_digest: str, dependencies: dict[str, str]) -> str:
    """Bind a distribution to its frozen recursive dependency approvals."""
    encoded = json.dumps({"schema": APPROVAL_SCHEMA, "package": package_digest,
                          "dependencies": dependencies}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stamp(info: os.stat_result) -> tuple:
    if _WINDOWS:
        # Windows derives permission bits from the file name for a path and from attributes for a
        # handle, and its st_ctime is the creation time: compare only what both report the same way.
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink,
                info.st_size, info.st_mtime_ns, getattr(info, "st_file_attributes", 0))
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_no_follow(path: str, *, directory: bool) -> int:
    """Open a Windows path without following a reparse point, as a read-only descriptor.

    The handle shares no delete access, so while it is held the entry (and, for a directory, every
    entry it is the parent of) cannot be renamed or swapped for a link. A file shares no write access
    either, so its bytes cannot change while they are read.
    """
    import _winapi
    import msvcrt

    access = 0x0081 if directory else 0x80000000  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES; GENERIC_READ
    share = 0x3 if directory else 0x1  # FILE_SHARE_READ | FILE_SHARE_WRITE; FILE_SHARE_READ
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
    handle = _winapi.CreateFile(path, access, share, 0, 3, flags, 0)  # 3 = OPEN_EXISTING
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


def capture_package(root: Path, *, destination: Path | None = None) -> PackageIdentity:
    """Hash bytes read through no-follow descriptors; optionally copy those same bytes.

    Directory and file identities are checked after reading. A concurrent change
    refuses capture rather than blessing a mixture. The digest covers executable
    bits but not writable bits, since the execution copy is always read-only.
    """
    base = Path(root)
    if base.is_symlink():
        raise PackageError("package root must not be a link")
    if not _WINDOWS and (not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd):
        raise PackageError("safe package capture is unavailable on this host")
    entries: list[dict] = []
    manifests: list[bytes] = []
    total = 0
    flags = os.O_RDONLY if _WINDOWS else os.O_RDONLY | os.O_NOFOLLOW

    # Windows has no dir_fd: entries are reached by path, under directory handles that are held
    # (and so cannot be renamed or replaced) for as long as their entries are read.
    def entry_stat(directory: int, location: str, name: str) -> os.stat_result:
        if not _WINDOWS:
            return os.stat(name, dir_fd=directory, follow_symlinks=False)
        info = os.stat(os.path.join(location, name), follow_symlinks=False)
        if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise PackageError("package links and special files are refused")
        return info

    def entry_open(directory: int, location: str, name: str, *, is_directory: bool) -> int:
        if _WINDOWS:
            return _open_no_follow(os.path.join(location, name), directory=is_directory)
        return os.open(name, flags | os.O_DIRECTORY if is_directory else flags, dir_fd=directory)

    def walk(directory: int, location: str, relative: str, target: Path | None) -> None:
        nonlocal total
        before = os.fstat(directory)
        names = sorted(os.listdir(location if _WINDOWS else directory))
        if len({name.casefold() for name in names}) != len(names):
            raise PackageError("case-colliding package paths")
        for name in names:
            path = f"{relative}/{name}" if relative else name
            if "\\" in name or len(path) > 1024:
                raise PackageError("ambiguous or oversized package path")
            if len(entries) >= MAX_FILES:
                raise PackageError("package file limit exceeded")
            info = entry_stat(directory, location, name)
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise PackageError("privileged package mode refused")
            output = target / name if target is not None else None
            if stat.S_ISDIR(info.st_mode):
                child = entry_open(directory, location, name, is_directory=True)
                try:
                    if _stamp(info) != _stamp(os.fstat(child)):
                        raise PackageError("package directory changed during capture")
                    entries.append({"path": path, "type": "directory"})
                    if output is not None:
                        output.mkdir(mode=0o700)
                    walk(child, os.path.join(location, name), path, output)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_BYTES:
                    raise PackageError("package byte limit exceeded")
                source = entry_open(directory, location, name, is_directory=False)
                digest = hashlib.sha256()
                size = 0
                content: list[bytes] = []
                writer = None
                try:
                    if _stamp(info) != _stamp(os.fstat(source)):
                        raise PackageError("package file changed during capture")
                    if output is not None:
                        writer = output.open("xb")
                    while chunk := os.read(source, 1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES or total + size > MAX_BYTES:
                            raise PackageError("package byte limit exceeded")
                        digest.update(chunk)
                        if writer is not None:
                            writer.write(chunk)
                        if path == ".codex-plugin/plugin.json":
                            if size > 1024 * 1024:
                                raise PackageError("plugin manifest is oversized")
                            content.append(chunk)
                    after = entry_stat(directory, location, name)
                    if _stamp(info) != _stamp(os.fstat(source)) or _stamp(info) != _stamp(after):
                        raise PackageError("package file changed during capture")
                finally:
                    if writer is not None:
                        writer.close()
                    os.close(source)
                total += size
                # Windows has no executable bit: its st_mode reports one from the file name's extension.
                executable = 0 if _WINDOWS else stat.S_IMODE(info.st_mode) & 0o111
                entries.append({"path": path, "type": "file", "bytes": size,
                                "executable": executable, "sha256": digest.hexdigest()})
                if output is not None:
                    output.chmod(0o444 | executable)
                if path == ".codex-plugin/plugin.json":
                    manifests.append(b"".join(content))
            else:
                raise PackageError("package links and special files are refused")
        if _stamp(before) != _stamp(os.fstat(directory)):
            raise PackageError("package directory changed during capture")
        if target is not None:
            target.chmod(0o555)

    descriptor = None
    try:
        if destination is not None:
            destination.mkdir(mode=0o700)
        if _WINDOWS:
            descriptor = _open_no_follow(str(base), directory=True)
        else:
            descriptor = os.open(base, flags | os.O_DIRECTORY)
        before = os.fstat(descriptor)
        if _WINDOWS and getattr(before, "st_file_attributes", 0) & _REPARSE_POINT:
            raise PackageError("package root must not be a link")
        walk(descriptor, str(base), "", destination)
        if _stamp(before) != _stamp(os.stat(base, follow_symlinks=False)):
            raise PackageError("package root changed during capture")
        if len(manifests) != 1:
            raise PackageError("package manifest is missing")
        manifest = json.loads(manifests[0])
        if not isinstance(manifest, dict):
            raise PackageError("package manifest must be an object")
        encoded = json.dumps({"schema": DIGEST_SCHEMA, "entries": entries},
                             sort_keys=True, separators=(",", ":")).encode()
        return PackageIdentity(hashlib.sha256(encoded).hexdigest(), manifest, tuple(entries))
    except (OSError, ValueError, RecursionError) as exc:
        raise PackageError(f"package capture failed ({type(exc).__name__})") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def execution_directory(parent: Path) -> Iterator[Path]:
    """Own one private snapshot directory and remove only that directory on return."""
    parent.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix="invocation-", dir=parent))
    try:
        yield owned
    finally:
        # Captured directories are sealed. Restore only our directories' write
        # bits to remove them, without following links or touching source trees.
        # Windows also refuses to delete a read-only file, so captured files get theirs back.
        # A junction is no symlink to os.walk or is_symlink(): never walk into or chmod one, or
        # the write bits of whatever user folder it points at would be restored too.
        for current, directories, files in os.walk(owned, followlinks=False):
            Path(current).chmod(0o700)
            directories[:] = [name for name in directories if not _is_reparse_point(Path(current) / name)]
            for name in directories + (files if _WINDOWS else []):
                path = Path(current) / name
                if not path.is_symlink() and not _is_reparse_point(path):
                    path.chmod(0o700)
        shutil.rmtree(owned)


def _is_reparse_point(path: Path) -> bool:
    """A symlink, junction or mount point on Windows (always False elsewhere)."""
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & _REPARSE_POINT)
    except OSError:
        return False
