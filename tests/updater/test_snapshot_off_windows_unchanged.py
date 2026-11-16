"""Off Windows the runtime snapshot and restore behave exactly as before the Windows long-path and link work.

The extended-length spelling and the link, junction and index checks are Windows only (owner rule: Mac and
Linux unchanged). Each row pins what fe546d26 did on this host; they pass there too, by design.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from core.updater import snapshot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pins the behaviour off Windows")

_WINDOWS_HELPERS = ("_io", "_extended_length", "_refuse_links_below", "_refuse_link_entry", "_refuse_links_from_home",
                    "_refuse_links_in_tree", "_copytree_refusing_links", "_walk_files", "_check_restore_paths")


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "note.txt").write_text("kept\n", encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "settings.json").write_text('{"kept": true}\n', encoding="utf-8")
    return home, data


def _index(source: Path, entries: list[dict]) -> None:
    (source / "index.json").write_text(json.dumps(entries), encoding="utf-8")


def test_the_windows_rules_are_off_on_this_host():
    assert getattr(snapshot, "_IS_WINDOWS", False) is False
    assert getattr(snapshot, "_GUARD_LINKS", False) is False


def test_a_round_trip_never_reaches_the_windows_helpers(tmp_path, monkeypatch):
    home, data = _home(tmp_path)
    with closing(sqlite3.connect(data / "chat.db")) as db, db:
        db.execute("CREATE TABLE chats(value TEXT)")
        db.execute("INSERT INTO chats VALUES ('kept')")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a Windows-only helper ran off Windows")

    for name in _WINDOWS_HELPERS:
        monkeypatch.setattr(snapshot, name, forbidden, raising=False)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    entries = [{"kind": "config", "path": "config", "present": True},
               {"kind": "sqlite", "path": "data/chat.db", "present": True},
               {"kind": "file", "path": "data/note.txt", "present": True}]
    assert (source / "index.json").read_text(encoding="utf-8") == json.dumps(entries, sort_keys=True) + "\n"
    (data / "note.txt").write_text("changed\n", encoding="utf-8")
    snapshot.restore_runtime(data, source)
    assert (data / "note.txt").read_text(encoding="utf-8") == "kept\n"


def test_a_symlinked_config_is_refused_with_the_old_message(tmp_path):
    home, data = _home(tmp_path)
    (home / "config" / "settings.json").rename(tmp_path / "settings.json")
    (home / "config").rmdir()
    (tmp_path / "real-config").mkdir()
    (home / "config").symlink_to(tmp_path / "real-config", target_is_directory=True)
    with pytest.raises(ValueError, match=r"^runtime config is a symlink; refusing an ambiguous backup$"):
        snapshot.snapshot_runtime(data, tmp_path / "snap")


def test_a_symlink_inside_the_config_is_refused_with_the_old_message(tmp_path):
    home, data = _home(tmp_path)
    (home / "config" / "link").symlink_to(home / "config" / "settings.json")
    with pytest.raises(ValueError, match=r"^runtime config contains a symlink; backup refused$"):
        snapshot.snapshot_runtime(data, tmp_path / "snap")


def test_a_symlink_in_the_data_is_refused_with_the_old_message(tmp_path):
    home, data = _home(tmp_path)
    (data / "link").symlink_to(data / "note.txt")
    with pytest.raises(ValueError, match=r"^runtime data contains a symlink; backup refused$"):
        snapshot.snapshot_runtime(data, tmp_path / "snap")


def test_a_restore_onto_a_symlink_is_refused_with_the_old_message(tmp_path):
    home, data = _home(tmp_path)
    source = tmp_path / "snap"
    snapshot.snapshot_runtime(data, source)
    (data / "note.txt").unlink()
    (data / "note.txt").symlink_to(data / "other.txt")
    with pytest.raises(ValueError, match=r"^restore destination became a symlink$"):
        snapshot.restore_runtime(data, source)


def test_a_restore_through_a_symlinked_folder_inside_the_home_still_writes_into_it(tmp_path):
    home, data = _home(tmp_path)
    (data / "inner").mkdir()
    (data / "link").symlink_to(data / "inner", target_is_directory=True)
    source = tmp_path / "snap"
    (source / "data" / "link").mkdir(parents=True)
    (source / "data" / "link" / "x.txt").write_text("restored\n", encoding="utf-8")
    _index(source, [{"path": "data/link/x.txt", "kind": "file", "present": True}])
    snapshot.restore_runtime(data, source)
    assert (data / "inner" / "x.txt").read_text(encoding="utf-8") == "restored\n"


def test_an_entry_of_unknown_kind_is_still_restored_as_a_file(tmp_path):
    home, data = _home(tmp_path)
    source = tmp_path / "snap"
    (source / "data").mkdir(parents=True)
    (source / "data" / "note.txt").write_text("old\n", encoding="utf-8")
    _index(source, [{"path": "data/note.txt", "kind": "folder", "present": True}])
    snapshot.restore_runtime(data, source)
    assert (data / "note.txt").read_text(encoding="utf-8") == "old\n"


def test_a_missing_saved_copy_is_still_a_file_not_found(tmp_path):
    home, data = _home(tmp_path)
    source = tmp_path / "snap"
    source.mkdir()
    _index(source, [{"path": "data/gone.txt", "kind": "file", "present": True}])
    with pytest.raises(FileNotFoundError, match=r"^snapshot missing: data/gone.txt$"):
        snapshot.restore_runtime(data, source)
