"""Consistent runtime DB/config and file-history backups at the update boundary."""
from __future__ import annotations

import json
import ntpath
import os
import shutil
import sqlite3
import stat
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from core import windows_path_policy

# Read at call time, so tests can drive the Windows spelling on any host.
_IS_WINDOWS = sys.platform == "win32"
_EXTENDED_PREFIX = "\\\\?\\"
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{base}{digit}" for base in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"})
_KINDS = frozenset({"config", "sqlite", "file"})


def _extended_length(text: str) -> str:
    r"""The ``\\?\`` spelling of a full local drive path, which has no MAX_PATH limit (WinError 3 past 260).

    Only ``X:\...`` is spelled. A share (``\\server\share``), a device or NT-namespace path (``\\.\``,
    ``\\?\UNC``, ``\\?\GLOBALROOT``), and a root-, drive- or plain-relative path raise ValueError before any
    file call: the updater never snapshots or restores over SMB, and unknown forms fail closed. The prefix
    turns off Win32 normalisation, so the path is normalised first and a component Win32 would have
    rewritten (``..``, a trailing dot or space, a reserved device name) is refused rather than created.
    An already extended drive path is checked the same way, without normalising.
    """
    if not isinstance(text, str):
        raise ValueError("a bytes path has no extended-length spelling")
    spelled = text.replace("/", "\\")
    if spelled.startswith(_EXTENDED_PREFIX):
        body = spelled[len(_EXTENDED_PREFIX):]
    else:
        if windows_path_policy.classify(spelled):
            raise ValueError(f"update file path is a share, device or relative path; refused: {text!r}")
        body = ntpath.normpath(spelled)
    drive, rest = ntpath.splitdrive(body)
    if not (len(drive) == 2 and drive[1] == ":" and drive[0].isascii() and drive[0].isalpha()
            and rest.startswith("\\")):
        raise ValueError(f"update file path is not a full local drive path; refused: {text!r}")
    for name in rest.split("\\"):
        if name and (name in (".", "..") or name != name.rstrip(" .")
                     or name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES):
            raise ValueError(f"update file path has a name Windows would rewrite; refused: {text!r}")
    return _EXTENDED_PREFIX + body


def _io(path: str | os.PathLike[str]) -> str:
    """The spelling handed to an OS call: extended-length on Windows, unchanged elsewhere.

    Containment checks and the snapshot index keep the plain spelling; only the OS call sees this one.
    """
    text = os.fspath(path)
    return _extended_length(text) if _IS_WINDOWS else text


def _refuse_unlistable(error: OSError) -> None:
    """A folder that cannot be listed would silently go missing from the backup or the link check."""
    raise error


_NAME_SURROGATE = 0x20000000  # IsReparseTagNameSurrogate: the reparse point stands for another named entry


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or on Windows a reparse point that redirects to another entry (junctions included).

    A junction is not is_symlink(), yet os.walk, copytree and the file calls follow it. A reparse point whose
    tag cannot be read counts as a link (fail closed); a non-surrogate one (a cloud-file placeholder, a
    deduplicated file) is the file itself and is kept.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(st, "st_reparse_tag", None)
    return not isinstance(tag, int) or bool(tag & _NAME_SURROGATE)


def _lstat_unless_missing(path: str | os.PathLike[str]) -> os.stat_result | None:
    try:
        return os.lstat(_io(path))
    except FileNotFoundError:
        return None


def _refuse_link_entry(path: Path, what: str) -> None:
    """Refuse when `path` itself is a symlink or junction; a missing entry is fine. Any other error propagates."""
    st = _lstat_unless_missing(path)
    if st is not None and _is_link(st):
        raise ValueError(f"{what} is a link or junction; refused: {path}")


def _refuse_links_below(base: Path, relative: Path, what: str) -> None:
    """Refuse a symlink or junction on any component of base/relative below `base`, checked top down.

    resolve() does not see a junction, and on Windows past MAX_PATH it cannot open a component at all and
    returns it unresolved, so each component is lstat'ed through its extended-length spelling. A missing
    component ends the walk (nothing below it exists yet); any other error propagates.
    """
    current = Path(base)
    for part in Path(relative).parts:
        current = current / part
        st = _lstat_unless_missing(current)
        if st is None:
            return
        if _is_link(st):
            raise ValueError(f"{what} crosses a link or junction; refused: {current}")


def _refuse_links_from_home(data_dir: Path, path: Path, what: str) -> None:
    """Refuse a link on every component from the VOOL home down to `path`, `path` included.

    The home is taken in both spellings: resolved, and as `data_dir` was spelled (an 8.3 short name such
    as RUNNER~1 does not compare equal to its long form). A path under neither is not walked from the home,
    so it is walked from its root instead: every component is judged, and none is taken on trust.
    """
    path = Path(os.path.abspath(path))
    homes = {Path(data_dir).resolve().parent, Path(os.path.abspath(data_dir)).parent}
    walked = False
    for home in homes:
        if path.is_relative_to(home):
            _refuse_links_below(home, path.relative_to(home), what)
            walked = True
    if not walked:
        _refuse_links_below(Path(path.anchor), path.relative_to(path.anchor), what)


def _refuse_link_names(directory: str, names: list[str], what: str) -> None:
    for name in names:
        if _is_link(os.lstat(os.path.join(directory, name))):
            raise ValueError(f"{what} contains a link or junction; refused: {os.path.join(directory, name)}")


def _refuse_links_in_tree(root: Path, what: str) -> None:
    """Refuse a symlink or junction anywhere below `root`. The walk lists the extended-length spelling."""
    for directory, dirs, names in os.walk(_io(root), followlinks=False, onerror=_refuse_unlistable):
        _refuse_link_names(directory, [*dirs, *names], what)


def _copytree_refusing_links(source: Path, target: Path, what: str) -> None:
    """copytree that re-checks each listing just before it is copied, so a link planted after the tree
    check is refused rather than followed (copytree(symlinks=False) follows a file link and a junction)."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        _refuse_link_names(directory, names, what)
        return set()

    shutil.copytree(_io(source), _io(target), ignore=ignore)


def _walk_files(data: Path) -> list[Path]:
    """Every regular file under `data` (update_v2 excluded), by its plain path; a link or junction is refused."""
    root = _io(data)
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=_refuse_unlistable):
        if directory == root:
            dirs[:] = [name for name in dirs if name != 'update_v2']
        _refuse_link_names(directory, [*dirs, *names], "runtime data")
        plain = data / directory[len(root):].lstrip("\\/") if directory != root else data
        files.extend(plain / name for name in names)
    return files


def snapshot_runtime(data_dir: Path, destination: Path) -> None:
    home = Path(data_dir).resolve().parent
    data = Path(data_dir).resolve()
    # The mirrored layout under data/update_v2/transactions/<txid>/runtime-snapshot can pass MAX_PATH on
    # Windows (WinError 3 with LongPathsEnabled=0), so every OS call takes the _io spelling.
    destination = Path(os.path.abspath(destination))
    _refuse_links_from_home(data_dir, destination, "snapshot destination")
    os.makedirs(_io(destination), exist_ok=True)
    os.chmod(_io(destination), 0o700)
    entries = []
    config = home / "config"
    if config.is_symlink():
        raise ValueError("runtime config is a symlink; refusing an ambiguous backup")
    _refuse_link_entry(config, "runtime config")
    present = _lstat_unless_missing(config) is not None
    entries.append({"path": "config", "kind": "config", "present": present})
    if present:
        _refuse_links_in_tree(config, "runtime config")
        _refuse_link_entry(destination / "config", "snapshot config")
        _copytree_refusing_links(config, destination / "config", "runtime config")
    files = _walk_files(data)
    databases = [p for p in files if p.suffix.lower() in ('.db', '.sqlite', '.sqlite3')]
    if len(databases) > 64 or len(files) > 100000 or sum(os.stat(_io(p)).st_size for p in files) > 8 * 1024**3:
        raise ValueError("runtime database backup exceeds limits")
    for database in sorted(databases):
        if not stat.S_ISREG(os.lstat(_io(database)).st_mode):
            raise ValueError("runtime DB backup requires regular files")
        relative = database.relative_to(home)
        target = destination / relative
        _refuse_links_below(destination, relative, "snapshot target")
        os.makedirs(_io(target.parent), exist_ok=True)
        deadline = time.monotonic() + 60

        def bounded(_status, _remaining, _total, deadline=deadline):
            if time.monotonic() > deadline:
                raise TimeoutError("SQLite snapshot did not finish in time")

        # Close both explicitly: 'with' alone commits but leaves the file open until GC, and Windows then
        # refuses the restore's rename over it.
        with closing(sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True, timeout=5)) as source:
            with closing(sqlite3.connect(_io(target))) as backup, backup:
                source.backup(backup, pages=256, progress=bounded, sleep=0.05)
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite snapshot failed its integrity check")
        os.chmod(_io(target), 0o600)
        entries.append({"path": relative.as_posix(), "kind": "sqlite", "present": True})
    sidecars = {Path(str(path) + suffix) for path in databases for suffix in ('-wal', '-shm', '-journal')}
    for path in sorted(set(files) - set(databases) - sidecars):
        if not stat.S_ISREG(os.lstat(_io(path)).st_mode):
            raise ValueError('runtime history backup requires regular files')
        relative = path.relative_to(home)
        target = destination / relative
        _refuse_links_below(destination, relative, "snapshot target")
        os.makedirs(_io(target.parent), exist_ok=True)
        shutil.copy2(_io(path), _io(target), follow_symlinks=False)
        os.chmod(_io(target), 0o600)
        entries.append({'path': relative.as_posix(), 'kind': 'file', 'present': True})
    with open(_io(destination / "index.json"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(entries, sort_keys=True) + "\n")


def _check_restore_paths(home: Path, source: Path, item: dict) -> None:
    """Refuse any link or junction on the way to the restore target, its staged name, or the saved copy.

    Restore never follows a link: a write through one could land outside the VOOL home, and a read through
    one would pull outside data into it.
    """
    relative = Path(item["path"])
    target = home / relative
    _refuse_links_below(home, relative, "restore destination")
    staged_name = ".vool-update-config-restore" if item["kind"] == "config" else target.name + ".vool-restore"
    _refuse_link_entry(target.with_name(staged_name), "restore staging path")
    _refuse_links_below(source, relative, "snapshot copy")
    if item["present"] and item["kind"] == "config":
        _refuse_links_in_tree(source / relative, "snapshot config")


def restore_runtime(data_dir: Path, source: Path) -> None:
    """Restore this snapshot's DB/config/history. Caller must have stopped the app.

    Keep backups intact if any operation fails; propagate the error to the status.
    WAL/SHM sidecars cannot accompany a restored database from another generation.
    """
    home = Path(data_dir).resolve().parent
    source = Path(os.path.abspath(source))
    _refuse_links_from_home(data_dir, source, "snapshot")
    with open(_io(source / "index.json"), encoding="utf-8") as handle:
        entries = json.loads(handle.read())
    for item in entries:
        if not isinstance(item.get("path"), str):
            raise ValueError("invalid snapshot index")
        if item.get("kind") not in _KINDS:
            raise ValueError("invalid snapshot index: unknown kind")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("invalid snapshot index")
        if item["kind"] == "config" and relative != Path("config"):
            raise ValueError("invalid snapshot index")
        target = home / relative
        target.resolve().relative_to(home)
        if target.is_symlink():
            raise ValueError("restore destination became a symlink")
        _check_restore_paths(home, source, item)
        saved = source / relative
        if item["present"] and _lstat_unless_missing(saved) is None:
            raise FileNotFoundError(f"snapshot missing: {relative}")
    for item in entries:
        target = home / item["path"]
        saved = source / item["path"]
        # Checked again right before the writes: a link planted since the first pass is refused.
        _check_restore_paths(home, source, item)
        if item["kind"] == "config":
            staged = target.with_name(".vool-update-config-restore")
            if _lstat_unless_missing(staged) is not None:
                shutil.rmtree(_io(staged))
            if item["present"]:
                _copytree_refusing_links(saved, staged, "snapshot config")
            if _lstat_unless_missing(target) is not None:
                shutil.rmtree(_io(target))
            if item["present"]:
                os.replace(_io(staged), _io(target))
        else:
            os.makedirs(_io(target.parent), exist_ok=True)
            staged = target.with_name(target.name + ".vool-restore")
            # A leftover staged entry is removed, never opened: copy2 would write through a link there.
            Path(_io(staged)).unlink(missing_ok=True)
            shutil.copy2(_io(saved), _io(staged), follow_symlinks=False)
            if item['kind'] == 'sqlite':
                for suffix in ("-wal", "-shm", '-journal'):
                    Path(_io(str(target) + suffix)).unlink(missing_ok=True)
            os.replace(_io(staged), _io(target))
