"""Durable writes keep working under Windows' fsync rules: no fsync on a read-only handle, no opened directory."""
from __future__ import annotations

import json

from tests import _windows_fsync_rules


def test_code_swap_journal_is_durable_under_windows_fsync_rules(tmp_path, monkeypatch):
    from installer.self_update import backup_and_swap, rollback_swap

    project, release, backup = tmp_path / "installed", tmp_path / "release", tmp_path / "backup"
    (project / "core").mkdir(parents=True)
    (project / "core" / "version").write_text("0.7.0")
    (release / "core").mkdir(parents=True)
    (release / "core" / "version").write_text("0.7.1")
    backup.mkdir()
    _windows_fsync_rules.apply(monkeypatch)
    assert backup_and_swap(release, project, set(), backup) == ["core"]
    assert json.loads((backup / "_replaced.json").read_text()) == ["core"]
    assert (project / "core" / "version").read_text() == "0.7.1"
    rollback_swap(project, backup)
    assert (project / "core" / "version").read_text() == "0.7.0"


def test_honesty_receipt_is_recorded_under_windows_fsync_rules(tmp_path, monkeypatch):
    from core import honesty_receipt
    from core.honesty_receipt import (
        VERDICT_CLEAN,
        issue_honesty_receipt,
        list_honesty_receipts,
        record_honesty_receipt,
    )

    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    receipt = issue_honesty_receipt(
        session_id="windows-fsync", turn_index=0, prompt_text="write hello.txt", response_text="Done.",
        claimed_actions=["file_write"],
        executed_tools=[{"tool": "workspace.write_file", "status": "executed", "receipt_key": "rk-1"}],
        verdict=VERDICT_CLEAN, verdict_detail="claim backed by executed tool receipt")
    _windows_fsync_rules.apply(monkeypatch)
    monkeypatch.setattr(honesty_receipt, "_WINDOWS", True, raising=False)
    first = record_honesty_receipt(receipt)
    assert (first.status, first.reason) == ("written", "")
    again = record_honesty_receipt(receipt)
    assert (again.status, again.reason) == ("already_exists", "")
    assert [row["receipt_id"] for row in list_honesty_receipts("windows-fsync")] == [receipt.receipt_id]
