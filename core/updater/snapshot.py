"""Consistent runtime DB/config and file-history backups at the update boundary."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from core.updater.longpath import io_path


def _refuse_unlistable(error: OSError) -> None:
    """A data folder that cannot be listed would silently miss from the backup: fail closed."""
    raise error


#: The entry kinds snapshot_runtime writes; restore refuses any other.
_RESTORE_KINDS = frozenset({"config", "sqlite", "file"})
#: A junction, a mount point or a symlink on Windows; `stat` names it only there.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _refuse_links_from_home(data_dir: Path, target: Path) -> None:
    """`_refuse_links_below` from the VOOL home, for a target spelled either way: from the resolved
    home, or from the home as the caller spelled `data_dir` (an 8.3 short name such as RUNNER~1 does
    not compare equal to its long form, and a target outside the home is not judged here)."""
    target = Path(os.path.abspath(target))
    for home in (Path(data_dir).resolve().parent, Path(os.path.abspath(data_dir)).parent):
        if target.is_relative_to(home):
            _refuse_links_below(home, target)


def _refuse_links_below(root: Path, target: Path) -> None:
    """Refuse `target` when any component from `root` down to it is a link, a junction or another
    reparse point, judged with lstat on the `io_path` spelling of each component.

    Past MAX_PATH, non-strict `Path.resolve()` leaves a deep junction unresolved (WinError 3 is in its
    allowed list, so the tail is kept as written), and `os.path.islink` is False for a junction, while
    the extended-length calls below follow it out of the VOOL home. This walk does not rely on either.
    A missing component ends the walk: nothing below it exists, and what is made there is a plain
    folder. Any other lstat error refuses (fail closed)."""
    relative = Path(os.path.abspath(target)).relative_to(root)
    current = Path(root)
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(io_path(current))
        except FileNotFoundError:
            return
        if _is_link(info):
            raise ValueError(f"update path passes through a link or junction; refused: {relative.as_posix()}")


def _refuse_links_in_tree(root: Path, message: str) -> None:
    """Refuse when anything inside the folder `root` is a link, a junction or another reparse point.

    lstat on every entry, not rglob or is_symlink: a junction is a link too, and os.walk, rglob and
    copytree all descend into one on Windows. The walk itself runs on the io_path spelling, so a deep
    tree is judged rather than skipped, and a folder it cannot list refuses."""
    for directory, dirs, names in os.walk(io_path(root), followlinks=False, onerror=_refuse_unlistable):
        if any(_is_link(os.lstat(os.path.join(directory, name))) for name in [*dirs, *names]):
            raise ValueError(message)


def snapshot_runtime(data_dir: Path, destination: Path) -> None:
    home = Path(data_dir).resolve().parent
    data = Path(data_dir).resolve()
    # Every destination-side call goes through io_path: on Windows the mirrored layout under
    # update_v2\transactions\<txid>\runtime-snapshot can pass MAX_PATH (WinError 3).
    destination = Path(os.path.abspath(destination))
    _refuse_links_from_home(data_dir, destination)
    os.makedirs(io_path(destination), exist_ok=True)
    os.chmod(io_path(destination), 0o700)
    entries = []
    config = home / "config"
    if config.is_symlink() or (os.path.lexists(config) and _is_link(os.lstat(config))):
        raise ValueError("runtime config is a symlink; refusing an ambiguous backup")
    entries.append({"path": "config", "kind": "config", "present": config.exists()})
    if config.exists():
        _refuse_links_in_tree(config, "runtime config contains a symlink; backup refused")
        shutil.copytree(io_path(config), io_path(destination / "config"))
    files = []
    for directory, dirs, names in os.walk(data, followlinks=False, onerror=_refuse_unlistable):
        if Path(directory) == data:
            dirs[:] = [name for name in dirs if name != 'update_v2']
        if any(_is_link(os.lstat(io_path(Path(directory) / name))) for name in [*dirs, *names]):
            # lstat, not is_symlink: a junction is a link too, and os.walk would descend into it.
            raise ValueError('runtime data contains a symlink; backup refused')
        files.extend(Path(directory) / name for name in names)
    databases = [p for p in files if p.suffix.lower() in ('.db', '.sqlite', '.sqlite3')]
    if len(databases) > 64 or len(files) > 100000 or sum(p.stat().st_size for p in files) > 8 * 1024**3:
        raise ValueError("runtime database backup exceeds limits")
    for database in sorted(databases):
        if database.is_symlink() or not database.is_file():
            raise ValueError("runtime DB backup requires regular files")
        relative = database.relative_to(home)
        target = destination / relative
        _refuse_links_below(destination, target)
        os.makedirs(io_path(target.parent), exist_ok=True)
        deadline = time.monotonic() + 60

        def bounded(_status, _remaining, _total, deadline=deadline):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite snapshot did not finish in time")

        # Close both explicitly: 'with' alone commits but leaves the file open until GC, and Windows then
        # refuses the restore's rename over it.
        with closing(sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True, timeout=5)) as source:
            with closing(sqlite3.connect(target)) as backup, backup:
                source.backup(backup, pages=256, progress=bounded, sleep=0.05)
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite snapshot failed its integrity check")
        os.chmod(io_path(target), 0o600)
        entries.append({"path": relative.as_posix(), "kind": "sqlite", "present": True})
    sidecars = {Path(str(path) + suffix) for path in databases for suffix in ('-wal', '-shm', '-journal')}
    for path in sorted(set(files) - set(databases) - sidecars):
        if not path.is_file():
            raise ValueError('runtime history backup requires regular files')
        relative = path.relative_to(home)
        target = destination / relative
        _refuse_links_below(destination, target)
        os.makedirs(io_path(target.parent), exist_ok=True)
        shutil.copy2(path, io_path(target))
        os.chmod(io_path(target), 0o600)
        entries.append({'path': relative.as_posix(), 'kind': 'file', 'present': True})
    with open(io_path(destination / "index.json"), "w", encoding="utf-8") as index:
        index.write(json.dumps(entries, sort_keys=True) + "\n")


def restore_runtime(data_dir: Path, source: Path) -> None:
    """Restore this snapshot's DB/config/history. Caller must have stopped the app.

    Keep backups intact if any operation fails; propagate the error to the status.
    WAL/SHM sidecars cannot accompany a restored database from another generation.
    """
    home = Path(data_dir).resolve().parent
    source = Path(os.path.abspath(source))
    with open(io_path(source / "index.json"), encoding="utf-8") as index:
        entries = json.loads(index.read())
    for item in entries:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid snapshot index")
        if item.get("kind") not in _RESTORE_KINDS:
            # An unknown kind would otherwise take the file branch and be copied over the target.
            raise ValueError(f"invalid snapshot index: unknown kind {item.get('kind')!r}")
        target = home / relative
        # Containment is judged on the logical paths; io_path spells only the OS call.
        target.resolve().relative_to(home)
        if os.path.islink(io_path(target)):
            raise ValueError("restore destination became a symlink")
        _refuse_links_below(home, target)
        saved = source / relative
        _refuse_links_from_home(data_dir, saved)
        _refuse_links_below(source, saved)
        if item["present"] and not os.path.exists(io_path(saved)):
            raise FileNotFoundError(f"snapshot missing: {relative}")
        if item["kind"] == "config" and item["present"]:
            _refuse_links_in_tree(saved, "saved runtime config contains a link or junction; restore refused")
    for item in entries:
        target = home / item["path"]
        saved = source / item["path"]
        # Checked again just before the writes: a link planted after the first pass must not be followed.
        _refuse_links_below(home, target)
        if item["kind"] == "config":
            staged = target.with_name(".vool-update-config-restore")
            if os.path.exists(io_path(staged)):
                shutil.rmtree(io_path(staged))
            if item["present"]:
                # The saved tree is walked again here: copytree follows a junction planted inside it.
                _refuse_links_in_tree(saved, "saved runtime config contains a link or junction; restore refused")
                shutil.copytree(io_path(saved), io_path(staged))
            if os.path.exists(io_path(target)):
                shutil.rmtree(io_path(target))
            if item["present"]:
                os.replace(io_path(staged), io_path(target))
        else:
            os.makedirs(io_path(target.parent), exist_ok=True)
            staged = target.with_name(target.name + ".vool-restore")
            _refuse_links_below(home, staged)
            shutil.copy2(io_path(saved), io_path(staged))
            if item['kind'] == 'sqlite':
                for suffix in ("-wal", "-shm", '-journal'):
                    _refuse_links_below(home, Path(str(target) + suffix))
                    Path(io_path(str(target) + suffix)).unlink(missing_ok=True)
            os.replace(io_path(staged), io_path(target))
