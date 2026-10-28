"""A runtime snapshot closes every SQLite handle it opened, so Windows can restore over those files later.

``with sqlite3.connect(...)`` commits but does not close, and a Connection sits in a reference cycle, so without an
explicit close the file stays open until the garbage collector runs. Windows refuses to rename over an open database.
"""
from __future__ import annotations

import gc
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from core.updater.snapshot import restore_runtime, snapshot_runtime


def _open_handles(path: Path) -> int:
    if Path("/proc/self/fd").is_dir():
        target = os.path.realpath(path)
        return sum(1 for fd in os.listdir("/proc/self/fd") if os.path.realpath(f"/proc/self/fd/{fd}") == target)
    # No descriptor table to read (Windows): renaming over an open file is refused there.
    stand_in = path.with_name(path.name + ".probe")
    stand_in.write_bytes(path.read_bytes())
    try:
        os.replace(stand_in, path)
    except PermissionError:
        stand_in.unlink()
        return 1
    return 0


def test_snapshot_leaves_no_database_open_so_restore_can_replace_it(tmp_path):
    home = tmp_path / "home"
    data = home / "data"
    data.mkdir(parents=True)
    database = data / "chat.db"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE chats(value TEXT)")
        db.execute("INSERT INTO chats VALUES ('kept')")
    snapshot = tmp_path / "snapshot"
    gc.disable()
    try:
        snapshot_runtime(data, snapshot)
        assert _open_handles(database) == 0
        assert _open_handles(snapshot / "data/chat.db") == 0
        restore_runtime(data, snapshot)
    finally:
        gc.enable()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT value FROM chats").fetchall() == [("kept",)]
