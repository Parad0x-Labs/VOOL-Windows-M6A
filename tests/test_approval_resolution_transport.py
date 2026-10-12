"""The served operator door preserves resolution faults instead of inventing stale tokens."""
from __future__ import annotations

import json

import pytest

from apps.vool_api_server import create_app
from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from core.web.api.runtime import RuntimeServices
from tests.asgi_harness import asgi_request
from tests.test_denied_approval_lifecycle import assert_closed, pending_write
from tests.test_denied_approval_lifecycle import isolated_runtime as isolated_runtime


def resolve(pending, *, decision="deny", approval_id=None):
    app = create_app(RuntimeServices())
    status, headers, body = asgi_request(app, method="POST", path="/api/mode",
        headers={"Content-Type": "application/json"}, body=json.dumps({
            "op": "resolve_approval", "session_id": pending.session,
            "turn_id": "operator-click", "scope": "once", "decision": decision,
            "approval_id": approval_id or pending.approval["approval_id"],
        }).encode())
    assert headers["x-request-id"]
    return status, json.loads(body)


def test_served_denial_closes_the_original_parent_and_child_without_dispatch(tmp_path, monkeypatch):
    from core import tool_intent_executor

    pending = pending_write(tmp_path)
    dispatches = []
    monkeypatch.setattr(tool_intent_executor, "execute_tool_intent", lambda **kwargs: dispatches.append(kwargs))
    status, body = resolve(pending)
    assert status == 200 and body["ok"] is True
    assert body["approval"]["status"] == "denied"
    assert_closed(pending)
    assert dispatches == []
    assert resolve(pending)[0] == 409
    events = runtime.list_runtime_session_events(pending.session, limit=200)
    assert sum(event["event_type"] == "approval_denial_terminal" for event in events) == 1


def test_served_missing_token_retains_the_actual_conflict(tmp_path):
    pending = pending_write(tmp_path)
    status, body = resolve(pending, approval_id="missing-token")
    assert status == 409 and body["fault_code"] == "conflict"
    assert "missing, stale, or already resolved" in body["error"]
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"


def test_served_invalid_decision_is_usage_not_stale(tmp_path):
    pending = pending_write(tmp_path)
    status, body = resolve(pending, decision="invalid")
    assert status == 400 and body["fault_code"] == "usage"
    assert "decision must be" in body["error"]
    assert policy._APPROVALS[pending.approval["approval_id"]]["status"] == "pending"


def test_served_reconciliation_failure_is_redacted_internal_not_stale(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    secret = "sk-synthetic-diagnostic-secret-only"

    def fail_store(**kwargs):
        raise OSError("receipt store fault: " + secret)

    monkeypatch.setattr(runtime, "store_tool_receipt", fail_store)
    status, body = resolve(pending)
    assert status == 500 and body["fault_code"] == "internal"
    assert "receipt store fault" in body["error"]
    assert secret not in json.dumps(body)
    assert "missing, stale" not in body["error"]
    assert "detail" not in body
    assert policy._APPROVALS[pending.approval["approval_id"]]["status"] == "denied"
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert not (pending.workspace / "canary.txt").exists()


def test_served_stale_execution_fence_is_not_reported_as_a_missing_token(tmp_path):
    from core.invocation.ledger import bump_generation

    pending = pending_write(tmp_path)
    bump_generation(pending.identity["execution_id"], runtime_epoch=pending.identity["runtime_epoch"])
    status, body = resolve(pending)
    assert status == 500 and body["fault_code"] == "internal"
    assert "missing, stale" not in body["error"]
    assert all(runtime.get_runtime_checkpoint(cid)["status"] == "pending_approval" for cid in pending.checkpoints)
    assert not (pending.workspace / "canary.txt").exists()


@pytest.mark.parametrize(("status", "code"), [(403, "permission_denied"), (409, "unavailable"),
                                            (403, "budget_refused")])
def test_served_adapter_preserves_registry_status_and_fault(tmp_path, monkeypatch, status, code):
    from core.command_registry import legacy

    pending = pending_write(tmp_path)
    monkeypatch.setattr(legacy, "forward", lambda *args, **kwargs: (status, {
        "ok": False, "summary": "resolution refused: " + code, "fault": {"code": code},
    }))
    actual_status, body = resolve(pending)
    assert actual_status == status and body["fault_code"] == code
    assert body["error"] == "resolution refused: " + code
    assert policy._APPROVALS[pending.approval["approval_id"]]["status"] == "pending"


@pytest.mark.parametrize(("status", "payload"), [(200, None), (200, []), (200, {}), (500, "invalid")])
def test_served_malformed_registry_result_is_internal_not_stale(tmp_path, monkeypatch, status, payload):
    from core.command_registry import legacy

    pending = pending_write(tmp_path)
    monkeypatch.setattr(legacy, "forward", lambda *args, **kwargs: (status, payload))
    actual_status, body = resolve(pending)
    assert actual_status == 500 and body["fault_code"] == "internal"
    assert body["error"] == "approval resolution failed"
