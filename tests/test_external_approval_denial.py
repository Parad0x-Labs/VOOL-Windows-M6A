"""Settings consent denial is not a fabricated runtime-turn finalization."""
from __future__ import annotations

import pytest

from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from tests.test_denied_approval_finalization import rows
from tests.test_denied_approval_lifecycle import assert_closed, pending_write
from tests.test_denied_approval_lifecycle import (
    isolated_runtime as isolated_runtime,
)


def external(**extra):
    return policy.register_external_approval({
        "task_id": "settings:synthetic-consent", "intent": "synthetic.spend_consent", **extra,
    })


@pytest.mark.parametrize("restore", [False, True])
def test_settings_denial_is_terminal_without_creating_runtime_work(restore):
    token = external()
    if restore:
        with policy._LOCK:
            policy._APPROVALS.clear()
            policy._PERSISTED_APPROVALS_RESTORED = False
        policy._ensure_approvals_restored()
    result = policy.resolve_approval(token, decision="deny")
    assert result["status"] == "denied" and result["scope"] == "once"
    assert result["approval_origin"] == "external"
    assert policy.resolve_approval(token, decision="deny") is None
    assert policy.resolve_approval(token, decision="allow") is None
    assert rows("SELECT * FROM a7_finalizations") == rows("SELECT * FROM semantic_admissions") == []
    assert rows("SELECT * FROM runtime_attempts") == rows("SELECT * FROM runtime_tool_receipts") == []
    assert policy._TASK_APPROVALS == []


def test_the_registration_door_owns_the_external_origin():
    token = external(approval_origin="runtime")
    assert policy.resolve_approval(token, decision="deny")["approval_origin"] == "external"


def test_an_external_label_never_exempts_real_runtime_ownership(tmp_path):
    pending = pending_write(tmp_path)
    with policy._LOCK:
        policy._APPROVALS[pending.approval["approval_id"]]["approval_origin"] = "external"
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny")["status"] == "denied"
    assert_closed(pending)
    final = rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (pending.identity["request_id"],))
    assert len(final) == 1 and final[0]["status"] == "no_answer_terminal"
    assert final[0]["terminal_reason"] == "permission_denied"
    assert final[0]["canonical_content"] == ""


def test_an_unmarked_missing_owner_still_fails_closed():
    token = external()
    with policy._LOCK:
        policy._APPROVALS[token].pop("approval_origin", None)
    with pytest.raises(ValueError, match="denial ownership is required"):
        policy.resolve_approval(token, decision="deny")
    assert policy._APPROVALS[token]["status"] == "denied"
    assert rows("SELECT * FROM a7_finalizations") == []


@pytest.mark.parametrize("binding", [None, {}, {"request_id": "synthetic-request"}])
def test_external_origin_with_a_runtime_binding_still_requires_ownership(binding):
    token = external(runtime_binding=binding)
    with pytest.raises(ValueError, match="denial ownership is required"):
        policy.resolve_approval(token, decision="deny")
    assert policy._APPROVALS[token]["status"] == "denied"
    assert rows("SELECT * FROM a7_finalizations") == []


@pytest.mark.parametrize("session", ["", "synthetic-chat"])
def test_external_origin_with_a_session_still_reconciles(monkeypatch, session):
    token = external(session_id=session)
    called = []

    def fail_closure(approval):
        called.append(approval["approval_id"])
        raise OSError("synthetic closure fault")

    monkeypatch.setattr(runtime, "reconcile_denied_approval", fail_closure)
    with pytest.raises(OSError, match="synthetic closure fault"):
        policy.resolve_approval(token, decision="deny")
    assert called == [token] and policy._APPROVALS[token]["status"] == "denied"
