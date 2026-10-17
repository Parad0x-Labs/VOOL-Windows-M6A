"""Denial diagnostics retain failure truth without sealing or executing work."""
from __future__ import annotations

import json

import pytest

from core import finalization
from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from core.invocation.ledger import FenceRefused
from core.secret_redaction import register_exact_secret
from tests.test_denied_approval_finalization import rows
from tests.test_denied_approval_lifecycle import (
    isolated_runtime as isolated_runtime,
)
from tests.test_denied_approval_lifecycle import pending_write


@pytest.mark.parametrize("error,reason", [
    (ValueError("denial obligation set belongs to a different request"), "denial_owner_mismatch"),
    (FenceRefused("fence refused"), "denial_execution_fence_refused"),
    (finalization.FinalizationRejected("", "DENIAL_OBLIGATIONS_OPEN", ""), "DENIAL_OBLIGATIONS_OPEN"),
    (finalization.FinalizationRejected("", "DENIAL_FINALIZATION_CONFLICT", ""), "DENIAL_FINALIZATION_CONFLICT"),
    (OSError("database unavailable"), "denial_finalization_failed"),
])
def test_finalization_failure_is_durable_and_original_exception_survives(tmp_path, monkeypatch, error, reason):
    pending = pending_write(tmp_path)

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(finalization, "finalize_denied_approval", fail)
    with pytest.raises(type(error)) as caught:
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert caught.value is error
    events = runtime.list_runtime_session_events(pending.session, limit=200)
    diagnostics = [event for event in events if event["event_type"] == "approval_denial_finalization_failed"]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["request_id"] == pending.identity["request_id"]
    assert diagnostic["attempt_id"] == pending.attempt_id
    assert diagnostic["task_id"] == pending.approval["task_id"]
    assert diagnostic["approval_id"] == pending.approval["approval_id"]
    assert diagnostic["obligation_set"] == [pending.obligations["set_id"], pending.obligations["version"]]
    assert diagnostic["exception_class"] == type(error).__name__
    assert diagnostic["failure_reason"] == reason
    assert diagnostic["exception_message"] == str(error)
    assert not any(event["event_type"] == "approval_denial_terminal" for event in events)
    assert rows("SELECT * FROM a7_finalizations") == rows("SELECT * FROM semantic_admissions") == []
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "CANCELLED"
    assert not (pending.workspace / "canary.txt").exists()
    for receipt in pending.receipts:
        assert runtime.load_tool_receipt(receipt)["execution"]["action_record"]["result"]["status"] == "permission_denied"


def test_diagnostic_redacts_before_bounding_and_does_not_store_a_traceback(tmp_path, monkeypatch):
    from core import secret_redaction

    monkeypatch.setattr(secret_redaction, "_exact_secrets", {})
    pending = pending_write(tmp_path)
    secret = "synthetic-diagnostic-credential-only"
    register_exact_secret(secret)
    error = OSError(secret + " password=synthetic-password-only " + "x" * 800)

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(finalization, "finalize_denied_approval", fail)
    with pytest.raises(OSError) as caught:
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert caught.value is error
    diagnostic = next(event for event in runtime.list_runtime_session_events(pending.session, limit=200)
                      if event["event_type"] == "approval_denial_finalization_failed")
    serialized = json.dumps(diagnostic)
    assert secret not in serialized and "synthetic-password-only" not in serialized
    assert len(diagnostic["exception_message"]) <= 512
    assert "traceback" not in serialized


def test_diagnostic_store_failure_cannot_replace_the_original_failure(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    original = ValueError("original finalization failure")
    real_append = runtime.append_runtime_event

    def fail(**kwargs):
        raise original

    def append(**kwargs):
        if kwargs.get("event_type") == "approval_denial_finalization_failed":
            raise OSError("diagnostic store failure")
        return real_append(**kwargs)

    monkeypatch.setattr(finalization, "finalize_denied_approval", fail)
    monkeypatch.setattr(runtime, "append_runtime_event", append)
    with pytest.raises(ValueError) as caught:
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert caught.value is original
    assert rows("SELECT * FROM a7_finalizations") == rows("SELECT * FROM semantic_admissions") == []
    assert not (pending.workspace / "canary.txt").exists()


def test_successful_denial_has_no_failure_diagnostic(tmp_path):
    pending = pending_write(tmp_path)
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    events = runtime.list_runtime_session_events(pending.session, limit=200)
    assert not any(event["event_type"] == "approval_denial_finalization_failed" for event in events)
    assert sum(event["event_type"] == "approval_denial_terminal" for event in events) == 1


def test_diagnostic_belongs_to_denied_request_not_the_newer_click_context(tmp_path, monkeypatch):
    from core.semantic.semantic_admissions import bound_execution_context, bound_request_context, current_request_id

    pending = pending_write(tmp_path)
    newer = pending_write(tmp_path, task="newer-turn")

    def fail(**kwargs):
        raise ValueError("diagnostic owner probe")

    monkeypatch.setattr(finalization, "finalize_denied_approval", fail)
    with bound_request_context(newer.identity["request_id"]), bound_execution_context(newer.identity):
        with pytest.raises(ValueError, match="diagnostic owner probe"):
            policy.resolve_approval(pending.approval["approval_id"], decision="deny")
        assert current_request_id() == newer.identity["request_id"]
    diagnostic = next(event for event in runtime.list_runtime_session_events(pending.session, limit=200)
                      if event["event_type"] == "approval_denial_finalization_failed")
    assert diagnostic["request_id"] == pending.identity["request_id"]
    assert diagnostic["obligation_set"] == [pending.obligations["set_id"], pending.obligations["version"]]
    assert runtime.get_runtime_attempt(newer.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"


def test_interrupted_finalization_recovers_once_without_rewriting_diagnostic(tmp_path, monkeypatch):
    from tests.test_denied_approval_lifecycle import assert_closed

    pending = pending_write(tmp_path)

    def fail(**kwargs):
        raise OSError("interrupted finalization")

    with monkeypatch.context() as patch:
        patch.setattr(finalization, "finalize_denied_approval", fail)
        with pytest.raises(OSError, match="interrupted finalization"):
            policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    before = rows("SELECT * FROM runtime_session_events WHERE event_type = 'approval_denial_finalization_failed'")
    assert len(before) == 1
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert before == rows("SELECT * FROM runtime_session_events WHERE event_type = 'approval_denial_finalization_failed'")
    assert len(rows("SELECT * FROM a7_finalizations")) == len(rows("SELECT * FROM semantic_admissions")) == 1
    assert_closed(pending)


def test_redaction_failure_preserves_original_exception_and_writes_no_unsafe_event(tmp_path, monkeypatch):
    from core import secret_redaction

    pending = pending_write(tmp_path)
    original = OSError("synthetic unrecorded diagnostic")
    real_redact = secret_redaction.redact_secrets

    def fail(**kwargs):
        raise original

    def redact(text):
        if text == str(original):
            raise ValueError("redaction fault")
        return real_redact(text)

    monkeypatch.setattr(finalization, "finalize_denied_approval", fail)
    monkeypatch.setattr(secret_redaction, "redact_secrets", redact)
    with pytest.raises(OSError) as caught:
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert caught.value is original
    assert rows("SELECT * FROM runtime_session_events WHERE event_type = 'approval_denial_finalization_failed'") == []
    assert rows("SELECT * FROM a7_finalizations") == []
