"""KAS-049: interrupted denial bookkeeping cannot mint duplicate accepted policy referents."""
import pytest

from core import finalization
from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from tests.test_denied_approval_finalization import rows
from tests.test_denied_approval_lifecycle import assert_closed, pending_write
from tests.test_denied_approval_lifecycle import isolated_runtime as isolated_runtime


def interrupted_denial(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    with monkeypatch.context() as patch:
        def interrupted(**kwargs):
            raise OSError("synthetic interruption after policy admission")

        patch.setattr(finalization, "no_answer_terminal", interrupted)
        try:
            policy.resolve_approval(pending.approval["approval_id"], decision="deny")
        except OSError as error:
            assert str(error) == "synthetic interruption after policy admission"
        else:
            raise AssertionError("Interrupted denial must preserve its failure")
    assert_closed(pending)
    assert rows("SELECT * FROM a7_finalizations") == []
    before = rows("SELECT * FROM semantic_admissions WHERE source_class = 'refusal_policy' AND accepted = 1")
    assert len(before) == 1
    return pending, before


@pytest.mark.parametrize("new_boot", [False, True])
def test_denial_recovers_after_admission_before_seal_without_duplicate_admissions(tmp_path, monkeypatch, new_boot):
    pending, before = interrupted_denial(tmp_path, monkeypatch)
    if new_boot:
        from core.semantic import semantic_result_seam

        monkeypatch.setattr(semantic_result_seam, "_BOOT_NONCE", "synthetic-new-boot")
    runtime.reconcile_denied_approval(policy._APPROVALS[pending.approval["approval_id"]])
    after = rows("SELECT * FROM semantic_admissions WHERE source_class = 'refusal_policy' AND accepted = 1")
    assert after == before
    final = rows("SELECT * FROM a7_finalizations")
    assert len(final) == 1 and final[0]["semantic_result_id"] == before[0]["sr_id"]
    assert final[0]["request_id"] == pending.identity["request_id"]
    assert final[0]["status"] == "no_answer_terminal" and final[0]["terminal_reason"] == "permission_denied"
    for _ in range(2):
        runtime.reconcile_denied_approval(policy._APPROVALS[pending.approval["approval_id"]])
    assert rows("SELECT * FROM a7_finalizations") == final
    assert rows("SELECT * FROM semantic_admissions WHERE source_class = 'refusal_policy' AND accepted = 1") == before
    assert_closed(pending)


@pytest.mark.parametrize("column,value", [
    ("request_id", "foreign-request"), ("source_class", "model"),
    ("obligation_set_version", "foreign-version"), ("accepted", 0),
])
def test_recovery_refuses_a_conflicting_or_rejected_policy_referent(tmp_path, monkeypatch, column, value):
    from storage.db import get_connection

    pending, before = interrupted_denial(tmp_path, monkeypatch)
    conn = get_connection()
    try:
        # Column names come only from the fixed parametrize allowlist above.
        conn.execute(f"UPDATE semantic_admissions SET {column} = ? WHERE sr_id = ?", (value, before[0]["sr_id"]))
        conn.commit()
    finally:
        conn.close()
    stored = rows("SELECT * FROM semantic_admissions")
    with pytest.raises(ValueError, match="denial admission conflicts"):
        runtime.reconcile_denied_approval(policy._APPROVALS[pending.approval["approval_id"]])
    assert rows("SELECT * FROM semantic_admissions") == stored
    assert rows("SELECT * FROM a7_finalizations") == []
    assert not any(event["event_type"] == "approval_denial_terminal"
        for event in runtime.list_runtime_session_events(pending.session, limit=200))
    assert_closed(pending)
