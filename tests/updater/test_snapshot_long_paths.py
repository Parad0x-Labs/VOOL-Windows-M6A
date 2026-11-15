"""CARRY item 1: the runtime snapshot and restore survive a deep VOOL home on Windows (MAX_PATH).

The extended-length spelling itself is pinned on every host by switching ``longpath._IS_WINDOWS`` on.
The round trip below builds a home whose snapshot targets pass 260 characters; on a Windows host with
LongPathsEnabled=0 (Steve) it fails with WinError 3 without the fix, elsewhere it checks the same flow.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.updater import longpath, snapshot

TXID = "1731550000-0.7.3-0123abcd"


def test_io_path_spells_only_full_local_drive_paths(monkeypatch):
    monkeypatch.setattr(longpath, "_IS_WINDOWS", True)
    assert longpath.io_path("C:/a/b") == "\\\\?\\C:\\a\\b"
    assert longpath.io_path("C:\\a\\x\\..\\b") == "\\\\?\\C:\\a\\b"
    assert longpath.io_path("\\\\?\\C:\\a\\b") == "\\\\?\\C:\\a\\b"
    refused = [
        "\\\\host\\share\\x", "//host/share/x", "\\\\?\\UNC\\host\\share\\x", "\\\\.\\pipe\\x",
        "\\x", "C:x", "rel\\x", "\\\\?\\C:\\a\\..\\b",
        "C:\\a\\CON", "C:\\a\\nul.txt", "C:\\a\\com1", "C:\\a\\b.", "C:\\a\\b ",
    ]
    for value in refused:
        with pytest.raises(ValueError):
            longpath.io_path(value)
    monkeypatch.setattr(longpath, "_IS_WINDOWS", False)
    assert longpath.io_path("rel/x") == "rel/x"
    assert longpath.io_path(Path("rel") / "x") == os.fspath(Path("rel") / "x")


def test_snapshot_and_restore_spell_their_file_calls_through_io_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / "data"
    (data / "history").mkdir(parents=True)
    (data / "history" / "note.txt").write_text("before\n", encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    seen: list[str] = []

    def recording(path):
        seen.append(os.fspath(path))
        return os.fspath(path)

    monkeypatch.setattr(snapshot, "io_path", recording)
    destination = tmp_path / "snap"
    snapshot.snapshot_runtime(data, destination)
    assert str(destination / "data" / "history" / "note.txt") in seen
    assert str(destination / "config") in seen
    seen.clear()
    snapshot.restore_runtime(data, destination)
    assert str(destination / "data" / "history" / "note.txt") in seen
    assert str(destination / "config") in seen


def test_runtime_snapshot_and_restore_past_max_path(tmp_path):
    home = tmp_path / "home"
    data = home / "data"
    deep = data
    while len(str(deep)) + 41 <= 225:  # every source folder stays creatable under MAX_PATH
        deep = deep / ("d" * 40)
    deep.mkdir(parents=True)
    note = deep / "history-note.txt"
    note.write_bytes(b"kept history\n")
    with closing(sqlite3.connect(data / "chat.db")) as db, db:
        db.execute("CREATE TABLE chats(value TEXT)")
        db.execute("INSERT INTO chats VALUES ('kept')")
    (home / "config").mkdir()
    (home / "config" / "settings.json").write_text('{"kept": true}\n', encoding="utf-8")
    destination = data / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    relative = note.relative_to(home)
    assert len(str(destination / relative)) > 260  # the row is not trivial: the mirrored target passes MAX_PATH

    snapshot.snapshot_runtime(data, destination)

    index = json.loads((destination / "index.json").read_text(encoding="utf-8"))
    recorded = {entry["path"] for entry in index}
    assert {"config", "data/chat.db", relative.as_posix()} <= recorded
    assert all("\\\\?\\" not in entry["path"] and not Path(entry["path"]).is_absolute() for entry in index)

    note.write_bytes(b"changed\n")
    with closing(sqlite3.connect(data / "chat.db")) as db, db:
        db.execute("UPDATE chats SET value = 'changed'")
    (home / "config" / "settings.json").write_text('{"kept": false}\n', encoding="utf-8")

    snapshot.restore_runtime(data, destination)

    assert note.read_bytes() == b"kept history\n"
    with closing(sqlite3.connect(data / "chat.db")) as db:
        assert db.execute("SELECT value FROM chats").fetchall() == [("kept",)]
    assert (home / "config" / "settings.json").read_text(encoding="utf-8") == '{"kept": true}\n'


def test_snapshot_refuses_a_data_folder_it_cannot_list(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / "data"
    (data / "locked").mkdir(parents=True)
    (data / "locked" / "kept.txt").write_text("kept\n", encoding="utf-8")
    (data / "open.txt").write_text("open\n", encoding="utf-8")
    real_scandir = os.scandir

    def guarded(path=".", *args, **kwargs):
        if not isinstance(path, int) and os.path.basename(os.fspath(path)) == "locked":
            raise PermissionError(13, "listing refused", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", guarded)
    destination = tmp_path / "snap"
    with pytest.raises(PermissionError):
        snapshot.snapshot_runtime(data, destination)
    assert not (destination / "index.json").exists()
