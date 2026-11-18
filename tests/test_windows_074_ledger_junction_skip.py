"""Windows: a private chat's erase and the startup sweep never follow a linked receipts_v2 kind folder.

The erase (and the Windows startup sweep, which removes the same files) lists the chat's receipts_v2
ledgers by walking the kind folders under data/receipts_v2. A kind folder that is a junction or a
symbolic link (a reparse point on Windows) was followed, so a planted link could point the delete at
a digest-named file outside the ledger folder. Security Review, 17 Nov (same-user only). On Windows
a kind folder that is a reparse point is now skipped. Off Windows nothing changes.
"""
from __future__ import annotations

import pytest

from core import private_chat_traces as traces
from core import windows_write_gate as gate
from core.evidence_kernel import receipts

SID = "openclaw:" + "f8" * 10


@pytest.fixture
def receipts_root(tmp_path, monkeypatch):
    root = tmp_path / "home" / "data" / "receipts_v2"
    (root / "vool.memory.packet.v1").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "vool.planted.v1").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(receipts, "_ledger_dir", lambda: root)
    monkeypatch.setattr(traces, "_honesty_ledger_files", lambda _sid: [])
    return root, outside


def _kinds(files) -> set[str]:
    return {path.parent.name for path in files}


def test_on_windows_a_linked_kind_folder_is_not_walked(receipts_root, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert _kinds(traces._ledger_files(SID)) == {"vool.memory.packet.v1"}


def test_on_windows_the_startup_sweep_leaves_the_file_behind_a_link(receipts_root, monkeypatch):
    root, outside = receipts_root
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    target = outside / receipts.ledger_path("vool.planted.v1", SID).name
    target.write_text("not a ledger of this home", encoding="utf-8")
    own = receipts.ledger_path("vool.memory.packet.v1", SID)
    own.write_text("x", encoding="utf-8")
    monkeypatch.setattr(traces, "_still_erasable", lambda _sid: True)
    monkeypatch.setattr(traces, "_tables", lambda _conn: {"private_chat_sessions"})
    monkeypatch.setattr(traces, "_waiting", lambda *_a: False)

    class _Conn:
        def execute(self, *_a):
            return [(SID,)]

        def close(self):
            pass

    monkeypatch.setattr(traces, "get_connection", lambda: _Conn())

    assert traces.sweep_receipt_files_at_startup() == 1
    assert not own.exists() and target.exists()


def test_off_windows_the_walk_is_as_before(receipts_root, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert _kinds(traces._ledger_files(SID)) == {"vool.memory.packet.v1", "vool.planted.v1"}
