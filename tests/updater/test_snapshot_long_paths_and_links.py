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
import pathlib
import shutil
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


# --- Shares and devices are refused, not spelled (SA review of windows/port-updater-longpath) ---------------

@pytest.mark.parametrize("refused", [
    "\\\\server\\share\\home\\data", "//server/share/home/data", "\\\\server\\share",
    "\\\\?\\UNC\\server\\share\\x", "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x",
    "\\\\.\\C:\\x", "\\\\.\\pipe\\x", "\\\\?\\C:\\a\\..\\b", "\\\\?\\C:x",
])
def test_windows_share_and_device_spellings_are_refused_before_any_file_call(monkeypatch, refused):
    monkeypatch.setattr(snapshot, "_IS_WINDOWS", True)
    with pytest.raises(ValueError, match="refused"):
        snapshot._io(refused)


# --- Both spellings of the home, and paths under neither (8.3 names such as RUNNER~1) -----------------------

def _aliased_home(tmp_path: Path) -> tuple[Path, Path]:
    """A home reached through a second spelling, the way an 8.3 short name reaches it on Windows."""
    real = tmp_path / "real"
    real_home, _ = _home(real)
    (tmp_path / "RUNNER~1").symlink_to(real, target_is_directory=True)
    return real_home, tmp_path / "RUNNER~1" / "home" / "data"


def test_a_snapshot_spelled_through_the_short_home_name_still_walks_from_the_home(tmp_path):
    real_home, data = _aliased_home(tmp_path)
    outside = _outside(tmp_path)
    transactions = data / "update_v2" / "transactions"
    transactions.mkdir(parents=True)
    (transactions / TXID).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(data, transactions / TXID / "runtime-snapshot")

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_a_snapshot_spelled_through_the_short_home_name_round_trips(tmp_path):
    real_home, data = _aliased_home(tmp_path)
    destination = data / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    snapshot.snapshot_runtime(data, destination)
    (real_home / "data" / "note.txt").write_text("changed\n", encoding="utf-8")
    snapshot.restore_runtime(data, destination)
    assert (real_home / "data" / "note.txt").read_text(encoding="utf-8") == "kept\n"


def test_a_snapshot_outside_the_home_through_a_link_is_refused_not_skipped(tmp_path):
    """A destination under neither spelling of the home is walked from its root, so a link on the way to it
    is refused (checking only the entry itself would let the writes follow the link)."""
    home, data = _home(tmp_path)
    outside = _outside(tmp_path)
    (tmp_path / "staging").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(data, tmp_path / "staging" / "snap")

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_a_restore_from_outside_the_home_through_a_link_is_refused_not_skipped(tmp_path):
    home, data = _home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    snapshot.snapshot_runtime(data, elsewhere / "snap")
    (data / "note.txt").write_text("current\n", encoding="utf-8")
    (tmp_path / "staging").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(data, tmp_path / "staging" / "snap")

    assert (data / "note.txt").read_text(encoding="utf-8") == "current\n"


# --- Folded in from windows/port-k4-updater-longpath (dbb06695) --------------------------------------------
# Each row plants a junction on Windows (_winapi.CreateJunction, no privilege needed) and a directory symlink
# elsewhere, so they run on every host; the faked-junction rows above are their Linux twins. lexical_resolve
# makes Path.resolve() keep the path as written, as it does past MAX_PATH on Windows, so the walk decides.

def _dir_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _no_walk(base, relative, what):
    return None


@pytest.fixture
def lexical_resolve(monkeypatch):
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, strict=False: type(self)(os.path.abspath(self)))


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    data = home / "data"
    deep = data
    for _ in range(4):
        deep = deep / ("d" * 30)
    deep.mkdir(parents=True)
    outside = tmp_path / "Documents"
    outside.mkdir()
    (outside / "victim.db").write_bytes(b"user database\n")
    (outside / "victim.db-wal").write_bytes(b"user wal\n")
    return {"home": home, "data": data, "deep": deep, "outside": outside}


def _outside_files(layout) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(layout["outside"].iterdir())}


def _transaction(layout) -> Path:
    return layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"


def _snapshot_naming(source: Path, relative: str, kind: str = "sqlite") -> None:
    saved = source / relative
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"from the snapshot\n")
    (source / "index.json").write_text(json.dumps([{"path": relative, "kind": kind, "present": True}]),
                                       encoding="utf-8")


def test_runtime_snapshot_and_restore_past_max_path_on_this_host(tmp_path):
    """On a Windows host with LongPathsEnabled=0 this fails with WinError 3 without the fix."""
    home, data = _home(tmp_path)
    deep = data
    while len(str(deep)) + 41 <= 225:  # every source folder stays creatable under MAX_PATH
        deep = deep / ("d" * 40)
    deep.mkdir(parents=True)
    note = deep / "history-note.txt"
    note.write_bytes(b"kept history\n")
    with closing(sqlite3.connect(data / "chat.db")) as db, db:
        db.execute("CREATE TABLE chats(value TEXT)")
        db.execute("INSERT INTO chats VALUES ('kept')")
    destination = _transaction({"data": data})
    relative = note.relative_to(home)
    assert len(str(destination / relative)) > 260

    snapshot.snapshot_runtime(data, destination)

    index = json.loads(open(snapshot._io(destination / "index.json"), encoding="utf-8").read())
    assert {"config", "data/chat.db", relative.as_posix()} <= {entry["path"] for entry in index}
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
    home, data = _home(tmp_path)
    (data / "locked").mkdir()
    (data / "locked" / "kept.txt").write_text("kept\n", encoding="utf-8")
    real_scandir = os.scandir

    def guarded(path=".", *args, **kwargs):
        if not isinstance(path, int) and os.path.basename(os.fspath(path)) == "locked":
            raise PermissionError(13, "listing refused", os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", guarded)
    with pytest.raises(PermissionError):
        snapshot.snapshot_runtime(data, tmp_path / "snap")
    assert not (tmp_path / "snap" / "index.json").exists()


def test_a_restore_through_a_deep_junction_is_refused_and_writes_nothing_outside(layout, lexical_resolve):
    junction = layout["deep"] / "J"
    _dir_link(junction, layout["outside"])
    _snapshot_naming(_transaction(layout), (junction / "victim.db").relative_to(layout["home"]).as_posix())
    before = _outside_files(layout)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], _transaction(layout))

    assert _outside_files(layout) == before


def test_sabotage_without_the_walk_the_restore_writes_through_the_junction(layout, lexical_resolve, monkeypatch):
    monkeypatch.setattr(snapshot, "_refuse_links_below", _no_walk)
    junction = layout["deep"] / "J"
    _dir_link(junction, layout["outside"])
    _snapshot_naming(_transaction(layout), (junction / "victim.db").relative_to(layout["home"]).as_posix())

    snapshot.restore_runtime(layout["data"], _transaction(layout))

    assert (layout["outside"] / "victim.db").read_bytes() == b"from the snapshot\n"
    assert not (layout["outside"] / "victim.db-wal").exists()


def test_a_restore_of_a_config_folder_that_is_a_junction_is_refused(layout, lexical_resolve):
    _dir_link(layout["home"] / "config", layout["outside"])
    source = _transaction(layout)
    (source / "config").mkdir(parents=True)
    (source / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "config", "kind": "config", "present": True}]),
                                       encoding="utf-8")
    before = _outside_files(layout)

    with pytest.raises(ValueError):
        snapshot.restore_runtime(layout["data"], source)

    assert _outside_files(layout) == before


def test_a_restore_whose_saved_copy_is_behind_a_junction_is_refused(layout, lexical_resolve):
    source = _transaction(layout)
    source.mkdir(parents=True)
    _dir_link(source / "data", layout["outside"])
    (source / "index.json").write_text(json.dumps([{"path": "data/victim.db", "kind": "sqlite", "present": True}]),
                                       encoding="utf-8")

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], source)

    assert not (layout["data"] / "victim.db").exists()


def test_a_snapshot_into_a_transaction_folder_that_is_a_junction_is_refused(layout):
    (layout["deep"] / "note.txt").write_text("history\n", encoding="utf-8")
    transactions = layout["data"] / "update_v2" / "transactions"
    _dir_link(transactions / TXID, layout["outside"])
    before = _outside_files(layout)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(layout["data"], transactions / TXID / "runtime-snapshot")

    assert _outside_files(layout) == before


def test_sabotage_without_the_walk_the_snapshot_writes_into_the_junction(layout, monkeypatch):
    monkeypatch.setattr(snapshot, "_refuse_links_below", _no_walk)
    (layout["deep"] / "note.txt").write_text("history\n", encoding="utf-8")
    transactions = layout["data"] / "update_v2" / "transactions"
    _dir_link(transactions / TXID, layout["outside"])

    snapshot.snapshot_runtime(layout["data"], transactions / TXID / "runtime-snapshot")

    assert (layout["outside"] / "runtime-snapshot" / "index.json").exists()


def test_a_snapshot_of_data_holding_a_junction_is_refused(layout):
    _dir_link(layout["deep"] / "J", layout["outside"])

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap")

    assert not (layout["home"].parent / "snap" / "index.json").exists()


def test_a_snapshot_and_restore_with_no_links_still_round_trip(layout):
    note = layout["deep"] / "note.txt"
    note.write_text("before\n", encoding="utf-8")
    snapshot.snapshot_runtime(layout["data"], _transaction(layout))
    note.write_text("changed\n", encoding="utf-8")
    snapshot.restore_runtime(layout["data"], _transaction(layout))
    assert note.read_text(encoding="utf-8") == "before\n"


def test_a_snapshot_whose_config_is_or_holds_a_junction_is_refused(layout):
    (layout["outside"] / "secret.txt").write_text("user secret\n", encoding="utf-8")
    _dir_link(layout["home"] / "config", layout["outside"])
    with pytest.raises(ValueError, match=r"symlink|link or junction"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap-a")
    if os.name == "nt":
        os.rmdir(layout["home"] / "config")  # removes the junction itself, not its target
    else:
        (layout["home"] / "config").unlink()
    (layout["home"] / "config").mkdir()
    _dir_link(layout["home"] / "config" / "J", layout["outside"])
    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap-b")
    assert not list(layout["home"].parent.glob("snap-*/config/**/secret.txt"))


def _saved_config_holding_a_junction(layout) -> Path:
    (layout["outside"] / "secret.txt").write_text("user secret\n", encoding="utf-8")
    (layout["home"] / "config").mkdir(parents=True)
    (layout["home"] / "config" / "settings.json").write_text('{"live": true}\n', encoding="utf-8")
    source = _transaction(layout)
    (source / "config").mkdir(parents=True)
    (source / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    _dir_link(source / "config" / "x", layout["outside"])
    (source / "index.json").write_text(json.dumps([{"path": "config", "kind": "config", "present": True}]),
                                       encoding="utf-8")
    return source


def test_a_restore_whose_saved_config_holds_a_junction_is_refused(layout):
    source = _saved_config_holding_a_junction(layout)
    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], source)
    assert not list((layout["home"] / "config").rglob("secret.txt"))
    assert (layout["home"] / "config" / "settings.json").read_text(encoding="utf-8") == '{"live": true}\n'


def test_sabotage_without_the_tree_walk_the_restore_copies_through_the_junction(layout, monkeypatch):
    """The control: with neither the saved tree walk nor the copy-time check, copytree follows the junction."""
    source = _saved_config_holding_a_junction(layout)
    real = snapshot._refuse_links_in_tree
    monkeypatch.setattr(snapshot, "_refuse_links_in_tree",
                        lambda root, what: None if Path(root) == source / "config" else real(root, what))
    monkeypatch.setattr(snapshot, "_copytree_refusing_links",
                        lambda src, dst, what: shutil.copytree(snapshot._io(src), snapshot._io(dst)))
    snapshot.restore_runtime(layout["data"], source)
    assert (layout["home"] / "config" / "x" / "secret.txt").read_text(encoding="utf-8") == "user secret\n"


def test_a_restore_with_an_entry_of_unknown_kind_is_refused_before_any_write(layout):
    (layout["data"] / "note.txt").write_text("live\n", encoding="utf-8")
    source = _transaction(layout)
    (source / "data").mkdir(parents=True)
    (source / "data" / "note.txt").write_text("old\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "data/note.txt", "kind": "folder", "present": True}]),
                                       encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        snapshot.restore_runtime(layout["data"], source)
    assert (layout["data"] / "note.txt").read_text(encoding="utf-8") == "live\n"
