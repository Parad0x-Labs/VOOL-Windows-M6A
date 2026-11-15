"""The runtime snapshot and restore refuse a path that passes through a junction or a link (K4 13, SA blocker).

The extended-length (`\\\\?\\`) spelling the snapshot now uses on Windows has no MAX_PATH limit, and the OS
follows a junction on it. Past MAX_PATH, non-strict `Path.resolve()` keeps a deep junction as written
(WinError 3 is in its allowed list) and `os.path.islink` is False for a junction, so the restore's
containment check passed and its unlink, copy and replace calls followed the junction out of the VOOL
home. Before the long-path change those calls failed with WinError 3. The fix walks every component
with lstat and refuses a link or any reparse point.

Each row plants a junction on Windows (`_winapi.CreateJunction`, which needs no privilege) and a
directory symlink elsewhere, and makes `Path.resolve()` keep the path as written, as it does past
MAX_PATH on Windows, so the walk is what decides on every host.
"""
from __future__ import annotations

import json
import os
import pathlib
from pathlib import Path

import pytest

from core.updater import snapshot

TXID = "1731550000-0.7.3-0123abcd"


def _dir_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def lexical_resolve(monkeypatch):
    """`Path.resolve()` as it behaves past MAX_PATH on Windows: the path comes back as written."""
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


def _snapshot_naming(source: Path, relative: str, kind: str = "sqlite") -> None:
    """A snapshot whose index names `relative`, with a saved copy of it."""
    saved = source / relative
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(b"from the snapshot\n")
    (source / "index.json").write_text(json.dumps([{"path": relative, "kind": kind, "present": True}]), encoding="utf-8")


def test_a_restore_through_a_deep_junction_is_refused_and_writes_nothing_outside(layout, lexical_resolve) -> None:
    junction = layout["deep"] / "J"
    _dir_link(junction, layout["outside"])
    relative = (junction / "victim.db").relative_to(layout["home"]).as_posix()
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    _snapshot_naming(source, relative)
    before = _outside_files(layout)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], source)

    assert _outside_files(layout) == before


def test_sabotage_without_the_walk_the_restore_writes_through_the_junction(layout, lexical_resolve, monkeypatch) -> None:
    """The control: with the component walk switched off, the same restore overwrites the user's file
    and deletes its -wal, which is the fail-open the walk closes."""
    monkeypatch.setattr(snapshot, "_refuse_links_below", lambda root, target: None)
    junction = layout["deep"] / "J"
    _dir_link(junction, layout["outside"])
    relative = (junction / "victim.db").relative_to(layout["home"]).as_posix()
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    _snapshot_naming(source, relative)

    snapshot.restore_runtime(layout["data"], source)

    assert (layout["outside"] / "victim.db").read_bytes() == b"from the snapshot\n"
    assert not (layout["outside"] / "victim.db-wal").exists()


def test_a_restore_of_a_config_folder_that_is_a_junction_is_refused(layout, lexical_resolve) -> None:
    _dir_link(layout["home"] / "config", layout["outside"])
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    (source / "config").mkdir(parents=True)
    (source / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "config", "kind": "config", "present": True}]), encoding="utf-8")
    before = _outside_files(layout)

    with pytest.raises(ValueError):
        snapshot.restore_runtime(layout["data"], source)

    assert _outside_files(layout) == before


def test_a_restore_whose_saved_copy_is_behind_a_junction_is_refused(layout, lexical_resolve) -> None:
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    source.mkdir(parents=True)
    _dir_link(source / "data", layout["outside"])
    (source / "index.json").write_text(json.dumps([{"path": "data/victim.db", "kind": "sqlite", "present": True}]), encoding="utf-8")

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], source)

    assert not (layout["data"] / "victim.db").exists()


def test_a_snapshot_into_a_transaction_folder_that_is_a_junction_is_refused(layout) -> None:
    (layout["deep"] / "note.txt").write_text("history\n", encoding="utf-8")
    transactions = layout["data"] / "update_v2" / "transactions"
    _dir_link(transactions / TXID, layout["outside"])
    before = _outside_files(layout)

    with pytest.raises(ValueError, match="link or junction"):
        snapshot.snapshot_runtime(layout["data"], transactions / TXID / "runtime-snapshot")

    assert _outside_files(layout) == before


def test_sabotage_without_the_walk_the_snapshot_writes_into_the_junction(layout, monkeypatch) -> None:
    monkeypatch.setattr(snapshot, "_refuse_links_below", lambda root, target: None)
    (layout["deep"] / "note.txt").write_text("history\n", encoding="utf-8")
    transactions = layout["data"] / "update_v2" / "transactions"
    _dir_link(transactions / TXID, layout["outside"])

    snapshot.snapshot_runtime(layout["data"], transactions / TXID / "runtime-snapshot")

    assert (layout["outside"] / "runtime-snapshot" / "index.json").exists()


def test_a_snapshot_of_data_holding_a_junction_is_refused(layout) -> None:
    _dir_link(layout["deep"] / "J", layout["outside"])

    with pytest.raises(ValueError, match="symlink"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap")

    assert not (layout["home"].parent / "snap" / "index.json").exists()


def test_a_snapshot_and_restore_with_no_links_still_round_trip(layout) -> None:
    note = layout["deep"] / "note.txt"
    note.write_text("before\n", encoding="utf-8")
    destination = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    snapshot.snapshot_runtime(layout["data"], destination)
    note.write_text("changed\n", encoding="utf-8")
    snapshot.restore_runtime(layout["data"], destination)
    assert note.read_text(encoding="utf-8") == "before\n"


def test_a_snapshot_whose_config_is_or_holds_a_junction_is_refused(layout) -> None:
    """The snapshot's read side: a junction as the config folder, or inside it, is not copied into the
    snapshot (os.walk, rglob and copytree all follow a junction on Windows)."""
    (layout["outside"] / "secret.txt").write_text("user secret\n", encoding="utf-8")
    _dir_link(layout["home"] / "config", layout["outside"])
    with pytest.raises(ValueError, match="symlink"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap-a")
    if os.name == "nt":
        os.rmdir(layout["home"] / "config")  # removes the junction itself, not its target
    else:
        (layout["home"] / "config").unlink()
    (layout["home"] / "config").mkdir()
    _dir_link(layout["home"] / "config" / "J", layout["outside"])
    with pytest.raises(ValueError, match="symlink"):
        snapshot.snapshot_runtime(layout["data"], layout["home"].parent / "snap-b")
    assert not list((layout["home"].parent).glob("snap-*/config/**/secret.txt"))


def _saved_config_holding_a_junction(layout) -> Path:
    """A snapshot whose saved config tree got a junction to the user's Documents after it was taken."""
    (layout["outside"] / "secret.txt").write_text("user secret\n", encoding="utf-8")
    (layout["home"] / "config").mkdir(parents=True)
    (layout["home"] / "config" / "settings.json").write_text("{\"live\": true}\n", encoding="utf-8")
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    (source / "config").mkdir(parents=True)
    (source / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    _dir_link(source / "config" / "x", layout["outside"])
    (source / "index.json").write_text(json.dumps([{"path": "config", "kind": "config", "present": True}]),
                                       encoding="utf-8")
    return source


def test_a_restore_whose_saved_config_holds_a_junction_is_refused(layout) -> None:
    """M1-13 review #2: copytree descends into a junction inside the saved tree, so the user's files
    would be copied into the VOOL config. The live config is left as it was."""
    source = _saved_config_holding_a_junction(layout)
    with pytest.raises(ValueError, match="link or junction"):
        snapshot.restore_runtime(layout["data"], source)
    assert not list((layout["home"] / "config").rglob("secret.txt"))
    assert (layout["home"] / "config" / "settings.json").read_text(encoding="utf-8") == "{\"live\": true}\n"


def test_sabotage_without_the_tree_walk_the_restore_copies_through_the_junction(layout, monkeypatch) -> None:
    """The control: with the saved tree not walked, copytree follows the junction on Windows (and copies
    a symlinked folder's contents elsewhere, since symlinks=False)."""
    source = _saved_config_holding_a_junction(layout)
    real = snapshot._refuse_links_in_tree
    monkeypatch.setattr(snapshot, "_refuse_links_in_tree",
                        lambda root, message: None if Path(root) == source / "config" else real(root, message))
    snapshot.restore_runtime(layout["data"], source)
    assert (layout["home"] / "config" / "x" / "secret.txt").read_text(encoding="utf-8") == "user secret\n"


def test_a_restore_with_an_entry_of_unknown_kind_is_refused_before_any_write(layout) -> None:
    """M2-13 review #3: an unknown kind would take the file branch and be copied over the target."""
    (layout["data"] / "note.txt").write_text("live\n", encoding="utf-8")
    source = layout["data"] / "update_v2" / "transactions" / TXID / "runtime-snapshot"
    (source / "data").mkdir(parents=True)
    (source / "data" / "note.txt").write_text("old\n", encoding="utf-8")
    (source / "index.json").write_text(json.dumps([{"path": "data/note.txt", "kind": "folder", "present": True}]),
                                       encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        snapshot.restore_runtime(layout["data"], source)
    assert (layout["data"] / "note.txt").read_text(encoding="utf-8") == "live\n"
