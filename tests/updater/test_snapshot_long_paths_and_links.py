"""Runtime snapshot and restore on a deep VOOL home, and never through a link (CARRY item 1).

On Windows with LongPathsEnabled=0 a file call past MAX_PATH (260 characters) fails with WinError 3, and the
snapshot mirrors the home's layout under data/update_v2/transactions/<txid>/runtime-snapshot. The rows below
switch the platform check on, so the Windows branch runs on any host, and pin that every OS call takes the
extended-length spelling. The link rows pin that restore refuses a symlink or junction instead of reading or
writing through it; a junction is simulated by the reparse attributes Windows reports for one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.updater import snapshot

TXID = "1731550000-0.7.3-0123abcd"
MOUNT_POINT = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: a directory junction
CLOUD_FILE = 0x9000001A  # IO_REPARSE_TAG_CLOUD_6: a OneDrive placeholder, the file itself


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "note.txt").write_text("kept\n", encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "settings.json").write_text('{"kept": true}\n', encoding="utf-8")
    return home, data


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("user file\n", encoding="utf-8")
    return outside


def _fake_junctions(monkeypatch, name: str, tag: int = MOUNT_POINT) -> None:
    """os.lstat reports every entry called `name` the way Windows reports a junction (or another reparse point)."""
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.path.basename(os.fspath(path)) != name:
            return st
        return SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size,
                               st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT, st_reparse_tag=tag)

    monkeypatch.setattr(os, "lstat", lstat)


@pytest.mark.parametrize(("plain", "spelled"), [
    ("C:\\Users\\steve\\" + "d" * 300 + "\\x.db", "\\\\?\\C:\\Users\\steve\\" + "d" * 300 + "\\x.db"),
    ("C:/Users/steve/home/data", "\\\\?\\C:\\Users\\steve\\home\\data"),
    ("C:\\a\\x\\..\\b", "\\\\?\\C:\\a\\b"),
    ("\\\\server\\share\\home\\data", "\\\\?\\UNC\\server\\share\\home\\data"),
    ("\\\\?\\C:\\already\\long", "\\\\?\\C:\\already\\long"),
])
def test_windows_paths_take_the_extended_length_spelling(monkeypatch, plain, spelled):
    monkeypatch.setattr(snapshot, "_IS_WINDOWS", True)
    assert snapshot._io(plain) == spelled


@pytest.mark.parametrize("refused", [
    "\\\\.\\pipe\\x", "\\x", "C:x", "rel\\x", "\\\\server", "C:\\a\\CON", "C:\\a\\nul.txt", "C:\\a\\com1",
    "C:\\a\\b.", "C:\\a\\b ",
])
def test_windows_paths_that_win32_would_rewrite_are_refused(monkeypatch, refused):
    monkeypatch.setattr(snapshot, "_IS_WINDOWS", True)
    with pytest.raises(ValueError):
        snapshot._io(refused)


def test_paths_off_windows_are_unchanged(monkeypatch):
    monkeypatch.setattr(snapshot, "_IS_WINDOWS", False)
    assert snapshot._io(Path("rel") / "x") == os.fspath(Path("rel") / "x")
    assert snapshot._io("C:\\a\\CON") == "C:\\a\\CON"


def test_snapshot_and_restore_past_max_path_spell_every_call_extended_on_windows(tmp_path, monkeypatch):
    home, data = _home(tmp_path)
    deep = data
    while len(str(deep)) < 200:
        deep = deep / ("d" * 40)
    deep.mkdir(parents=True)
    note = deep / "history-note.txt"
    note.write_bytes(b"kept history\n")
    with closing(sqlite3.connect(deep / "chat.db")) as db, db:
        db.execute("CREATE TABLE chats(value TEXT)")
        db.execute("INSERT INTO chats VALUES ('kept')")
    destination = data / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    deep_target = destination / note.relative_to(home)
    assert len(str(deep_target)) > 260
    windows_target = "C:\\Users\\steve\\" + str(deep_target.relative_to(tmp_path)).replace("/", "\\")
    assert len(windows_target) > 260
    assert snapshot._extended_length(windows_target) == "\\\\?\\" + windows_target

    # The Windows branch runs on this host: record what it is handed, and hand the host its own spelling back.
    seen: list[str] = []

    def recording(text):
        seen.append(text)
        return text

    monkeypatch.setattr(snapshot, "_IS_WINDOWS", True)
    monkeypatch.setattr(snapshot, "_extended_length", recording)
    snapshot.snapshot_runtime(data, destination)
    assert str(deep_target) in seen
    assert str(destination / "data" / deep.relative_to(data) / "chat.db") in seen
    index = json.loads((destination / "index.json").read_text(encoding="utf-8"))
    assert {"config", note.relative_to(home).as_posix(), (deep / "chat.db").relative_to(home).as_posix()} <= {
        entry["path"] for entry in index}
    assert all("\\\\?\\" not in entry["path"] for entry in index)

    note.write_bytes(b"changed\n")
    with closing(sqlite3.connect(deep / "chat.db")) as db, db:
        db.execute("UPDATE chats SET value = 'changed'")
    seen.clear()
    snapshot.restore_runtime(data, destination)
    assert str(deep_target) in seen
    assert str(note) in seen
    assert note.read_bytes() == b"kept history\n"
    with closing(sqlite3.connect(deep / "chat.db")) as db:
        assert db.execute("SELECT value FROM chats").fetchall() == [("kept",)]


def test_restore_does_not_write_through_a_staged_link_to_outside(tmp_path):
    home, data = _home(tmp_path)
    outside = _outside(tmp_path)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    (data / "note.txt.vool-restore").symlink_to(outside / "secret.txt")

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert (outside / "secret.txt").read_text(encoding="utf-8") == "user file\n"


def test_restore_does_not_follow_a_link_inside_the_saved_config(tmp_path):
    home, data = _home(tmp_path)
    outside = _outside(tmp_path)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    (source / "config" / "x").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert sorted(p.name for p in (home / "config").iterdir()) == ["settings.json"]
    assert not (home / ".vool-update-config-restore").exists()


def test_restore_does_not_read_a_saved_file_through_a_link(tmp_path):
    home, data = _home(tmp_path)
    outside = _outside(tmp_path)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    (data / "note.txt").write_text("current\n", encoding="utf-8")
    (source / "data" / "note.txt").unlink()
    (source / "data" / "note.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert (data / "note.txt").read_text(encoding="utf-8") == "current\n"


def test_restore_refuses_a_link_component_even_one_that_stays_in_the_home(tmp_path):
    home, data = _home(tmp_path)
    inner = data / "inner"
    inner.mkdir()
    (data / "link").symlink_to(inner, target_is_directory=True)
    source = tmp_path / "snap"
    (source / "data" / "link").mkdir(parents=True)
    (source / "data" / "link" / "x.txt").write_text("restored\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "data/link/x.txt", "kind": "file", "present": True}]),
                                       encoding="utf-8")

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert list(inner.iterdir()) == []


def test_restore_refuses_a_junction_on_the_way_to_the_target(tmp_path, monkeypatch):
    home, data = _home(tmp_path)
    (data / "J").mkdir()  # Windows would follow this junction wherever it points
    source = tmp_path / "snap"
    (source / "data" / "J").mkdir(parents=True)
    (source / "data" / "J" / "x.txt").write_text("restored\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "data/J/x.txt", "kind": "file", "present": True}]),
                                       encoding="utf-8")
    _fake_junctions(monkeypatch, "J")

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert list((data / "J").iterdir()) == []


def test_snapshot_refuses_a_junction_in_the_runtime_data(tmp_path, monkeypatch):
    home, data = _home(tmp_path)
    (data / "J").mkdir()
    (data / "J" / "elsewhere.txt").write_text("user file\n", encoding="utf-8")
    source = tmp_path / "snap"
    _fake_junctions(monkeypatch, "J")

    with pytest.raises(ValueError):
        snapshot.snapshot_runtime(data, source)

    assert not (source / "data" / "J").exists()
    assert not (source / "index.json").exists()


def test_snapshot_keeps_a_cloud_placeholder_which_is_not_a_link(tmp_path, monkeypatch):
    home, data = _home(tmp_path)
    source = tmp_path / "snap"
    _fake_junctions(monkeypatch, "note.txt", tag=CLOUD_FILE)

    snapshot.snapshot_runtime(data, source)

    assert (source / "data" / "note.txt").read_text(encoding="utf-8") == "kept\n"


def test_snapshot_refuses_a_destination_reached_through_a_link(tmp_path):
    home, data = _home(tmp_path)
    outside = _outside(tmp_path)
    (data / "update_v2").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        snapshot.snapshot_runtime(data, data / "update_v2" / "transactions" / TXID / "runtime-snapshot")

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


@pytest.mark.parametrize("entry", [
    {"path": "data/note.txt", "kind": "script", "present": True},
    {"path": "data", "kind": "config", "present": True},
    {"path": ".", "kind": "file", "present": True},
])
def test_restore_refuses_an_index_entry_it_does_not_know(tmp_path, entry):
    home, data = _home(tmp_path)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    (data / "note.txt").write_text("current\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([entry]), encoding="utf-8")

    with pytest.raises(ValueError):
        snapshot.restore_runtime(data, source)

    assert (data / "note.txt").read_text(encoding="utf-8") == "current\n"
    assert (data / "note.txt").is_file()
