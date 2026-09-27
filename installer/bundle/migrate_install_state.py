"""Move legacy program-folder state to the per-user home before installer cleanup.

This runs after the new bundle is extracted and before the Finish page. A conflict,
reparse point, or failed copy leaves the legacy source in place and fails setup.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path


class MigrationError(RuntimeError):
    pass


def _redirected(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _collect(directory: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    directories = [directory]

    def visit(parent: Path) -> None:
        if _redirected(parent):
            raise MigrationError("legacy state contains a redirected directory")
        with os.scandir(parent) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _redirected(path):
                    raise MigrationError("legacy state contains a redirected path")
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    directories.append(path)
                    visit(path)
                elif stat.S_ISREG(mode):
                    files.append(path)
                else:
                    raise MigrationError("legacy state contains an unsupported file type")

    visit(directory)
    return files, directories


def _digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _safe_destination(home: Path, target: Path) -> None:
    if _redirected(home):
        raise MigrationError("per-user home is redirected")
    parent = target.parent
    while parent != home:
        if not parent.is_relative_to(home):
            raise MigrationError("migration target escaped the per-user home")
        if _redirected(parent):
            raise MigrationError("per-user destination contains a redirected directory")
        parent = parent.parent
    if _redirected(target):
        raise MigrationError("per-user destination contains a redirected file")


def migrate_legacy_home(root: Path, home: Path) -> int:
    legacy = root / "app" / ".vool_local"
    if not legacy.exists() and not _redirected(legacy):
        return 0
    if not legacy.is_dir() or _redirected(legacy):
        raise MigrationError("legacy home is not a regular directory")
    if home.resolve().is_relative_to(root.resolve()):
        raise MigrationError("per-user home overlaps the program folder")
    if home.exists() and (not home.is_dir() or _redirected(home)):
        raise MigrationError("per-user home is not a regular directory")

    files, directories = _collect(legacy)
    for source in files:
        destination = home / source.relative_to(legacy)
        _safe_destination(home, destination)
        if destination.exists() and (not destination.is_file() or _digest(source) != _digest(destination)):
            raise MigrationError("legacy and per-user state conflict; both were preserved")

    home.mkdir(parents=True, exist_ok=True)
    for source in files:
        destination = home / source.relative_to(legacy)
        _safe_destination(home, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        temporary = destination.with_name(destination.name + f".vool-migrate-{os.getpid()}")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if _digest(source) != _digest(temporary):
                raise MigrationError("legacy state copy did not verify")
            # A hard link is an atomic create-if-absent on the same volume.
            # Never use os.replace here: it could overwrite newer user state.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if _digest(source) != _digest(destination):
                    raise MigrationError("per-user state changed during migration")
        finally:
            temporary.unlink(missing_ok=True)

    for source in files:
        destination = home / source.relative_to(legacy)
        if _digest(source) != _digest(destination):
            raise MigrationError("legacy state changed during verification")
    for source in files:
        source.unlink()
    for directory in reversed(directories):
        directory.rmdir()
    return len(files)


def remove_generated_bytecode(root: Path) -> int:
    app = root / "app"
    removed = 0
    for parent, dirs, _files in os.walk(app, followlinks=False):
        if _redirected(Path(parent)):
            raise MigrationError("program folder contains a redirected directory")
        for name in list(dirs):
            if _redirected(Path(parent) / name):
                dirs.remove(name)
                continue
            if name != "__pycache__":
                continue
            cache = Path(parent) / name
            files, directories = _collect(cache)
            if len(directories) != 1 or any(path.suffix != ".pyc" for path in files):
                raise MigrationError("generated cache contains non-bytecode data")
            for path in files:
                path.unlink()
                removed += 1
            cache.rmdir()
            dirs.remove(name)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        migrate_legacy_home(args.root, args.home)
        remove_generated_bytecode(args.root)
    except (MigrationError, OSError) as exc:
        print(f"VOOL program-state migration stopped safely: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
