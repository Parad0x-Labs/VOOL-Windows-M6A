"""Operator Deny closes only its pending runtime work; it never dispatches a tool."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from core.agent_runtime.orchestrator import emit_runtime_event
from core.semantic.semantic_admissions import bound_execution_context, bound_request_context


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    from core import runtime_paths
    from storage.db import configure_default_db_path, reset_default_connection
    from storage.migrations import run_migrations

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    configure_default_db_path(tmp_path / "test.db")
    reset_default_connection()
    runtime.configure_runtime_continuity_db_path(tmp_path / "test.db")
    run_migrations()
    policy.reset_mode_permission_state()
    yield
    policy.reset_mode_permission_state()
    runtime.reset_runtime_continuity_state()
    runtime.configure_runtime_continuity_db_path(None)
    configure_default_db_path(None)
    reset_default_connection()
    runtime_paths.configure_runtime_home(None)


def pending_write(tmp_path, *, task="task-denied", session="chat-denied",
        request="Create the canary.", prose_obligation=False):
    from contextvars import copy_context

    from core.conductor import obligation_ledger as ledger
    from core.invocation.ledger import accept_invocation, current_runtime_epoch, open_execution
    from core.semantic.bridges.ledger import demand_obligation_rows, snapshot_graph_payload
    from core.semantic.producers.lexical import lexical_request_graph

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    request_id = accept_invocation(external_kind="http", external_value=task,
        principal="owner_local", session_binding=session)["request_id"]
    context = {"runtime_session_id": session, "request_id": request_id,
               "cancel_turn_id": task, "turn_id": task,
               "workspace_root": str(workspace), "workspace": str(workspace), "operating_mode": "manual"}
    policy.set_active_mode(session, "manual", client_turn_id=task)
    with bound_request_context(request_id):
        attempt = runtime.create_runtime_attempt(session_id=session, original_request=request,
            origin_user_turn_id=task, trigger_user_turn_id=task, role=runtime.AttemptRole.TURN_ROOT.value)
        execution = open_execution(request_id=request_id, root_attempt_id=attempt["attempt_id"])
        identity = {"attempt_id": attempt["attempt_id"], "execution_id": execution["execution_id"],
                    "generation": execution["generation"], "runtime_epoch": current_runtime_epoch(),
                    "request_id": request_id}
        context["_execution_identity"] = identity
        graph = lexical_request_graph(request, turn_id=task)
        obligations = ledger.open_obligation_set(request_id=request_id, request_text=request,
            request_graph=snapshot_graph_payload(graph),
            obligations=[*demand_obligation_rows(graph, attempt_id=attempt["attempt_id"], request_text=request),
                ({"obligation_id": f"ob:{attempt['attempt_id']}:answer", "kind": "prose", "text": request}
                 if prose_obligation else
                 {"obligation_id": "effect-canary", "kind": "effect", "text": "Create the canary file."})])
        with bound_execution_context(identity):
            def decide():
                ledger.bind_active_set(obligations["set_id"], obligations["version"])
                return policy.decide_tool_call(intent="workspace.write_file", task_id=task,
                    arguments={"path": "canary.txt", "content": "M6B_SYNTHETIC_CANARY"}, source_context=context)

            decision = copy_context().run(decide)
    approval = decision.approval_request
    assert approval is not None
    checkpoints, receipt_ids = [], []
    for label in ("child", "parent"):
        checkpoint = runtime.create_runtime_checkpoint(session_id=session, request_text=request,
            task_id=task, task_class="coding", source_context=context)
        checkpoints.append(checkpoint["checkpoint_id"])
        child = {**context, "runtime_checkpoint_id": checkpoint["checkpoint_id"], "turn_id": task + label}
        action = emit_runtime_event(SimpleNamespace(_runtime_checkpoint_id=lambda ctx: ctx["runtime_checkpoint_id"]),
            child, event_type="tool_preview", message="Waiting for approval.",
            emit_runtime_event_fn=lambda ctx, **event: runtime.append_runtime_event(session_id=session, **event),
            tool_name="workspace.write_file", task_id=task, status="pending_approval", mode="tool_preview",
            approval_request=approval, diff_preview=approval.get("diff_preview"),
            arguments={"path": "canary.txt", "content": "M6B_SYNTHETIC_CANARY"})
        receipt_ids.append(action["receipt_id"])
        runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], status="pending_approval",
            pending_intent={"intent": "workspace.write_file", "approval_request": approval},
            state={"pending_tool_payload": {"path": "canary.txt"}})
    runtime.update_runtime_attempt(attempt["attempt_id"], lifecycle_state="WAITING_APPROVAL", completed=False)
    return SimpleNamespace(approval=approval, checkpoints=checkpoints, receipts=receipt_ids,
        attempt_id=attempt["attempt_id"], session=session, identity=identity, workspace=workspace,
        obligations=obligations)


def assert_closed(pending):
    from core.secret_redaction import redact_secrets

    attempt = runtime.get_runtime_attempt(pending.attempt_id)
    assert attempt["lifecycle_state"] == "CANCELLED"
    assert attempt["terminal_reason"] == "permission_denied"
    assert attempt["retryable"] is False
    assert attempt["completed_at"]
    for checkpoint_id in pending.checkpoints:
        checkpoint = runtime.get_runtime_checkpoint(checkpoint_id)
        assert checkpoint["status"] == "cancelled"
        assert checkpoint["pending_intent"] == {}
        assert checkpoint["state"]["pending_tool_payload"] is None
        assert checkpoint["outcome"]["fulfillment_status"] == "cancelled"
        assert checkpoint["outcome"]["failure_codes"] == ["permission_denied"]
    for receipt_id in pending.receipts:
        record = runtime.load_tool_receipt(receipt_id)["execution"]["action_record"]
        assert record["receipt_id"] == redact_secrets(receipt_id)
        assert record["result"]["outcome"] == "failed"
        assert record["result"]["status"] == "permission_denied"
        assert record["result"]["ok"] is False
        assert record["approval"]["approval_state"] == "denied"
        assert record["approval"]["approval_id"] == pending.approval["approval_id"]
    assert not (pending.workspace / "canary.txt").exists()


def test_denial_closes_both_previews_and_the_original_waiting_attempt(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    from core import tool_intent_executor
    dispatch = []
    monkeypatch.setattr(tool_intent_executor, "execute_tool_intent", lambda **kwargs: dispatch.append(kwargs))
    result = policy.resolve_approval(pending.approval["approval_id"], decision="deny", scope="project")
    assert result["status"] == "denied" and result["scope"] == "once"
    assert_closed(pending)
    assert dispatch == []


def test_duplicate_denial_does_not_duplicate_receipts_or_touch_a_newer_turn(tmp_path):
    denied = pending_write(tmp_path)
    newer = pending_write(tmp_path, task="task-newer")
    with bound_request_context("req:http:task-newer"), bound_execution_context(newer.identity):
        assert policy.resolve_approval(denied.approval["approval_id"], decision="deny")
    assert policy.resolve_approval(denied.approval["approval_id"], decision="deny") is None
    assert policy.resolve_approval(denied.approval["approval_id"], decision="allow") is None
    assert_closed(denied)
    assert runtime.get_runtime_attempt(newer.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert all(runtime.get_runtime_checkpoint(cid)["status"] == "pending_approval" for cid in newer.checkpoints)
    events = runtime.list_runtime_session_events(denied.session, limit=200)
    terminals = [event for event in events if event["event_type"] == "approval_denial_terminal"]
    assert len(terminals) == 1
    assert terminals[0]["effect_count"] == 0
    assert terminals[0]["approval_id"] == denied.approval["approval_id"]


def test_allow_keeps_the_existing_resume_contract(tmp_path):
    pending = pending_write(tmp_path)
    assert policy.resolve_approval(pending.approval["approval_id"], decision="allow")["status"] == "approved"
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert all(runtime.get_runtime_checkpoint(cid)["status"] == "pending_approval" for cid in pending.checkpoints)


def test_partial_bookkeeping_failure_can_be_reconciled_without_a_second_decision(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    real_store = runtime.store_tool_receipt
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "store_tool_receipt", lambda **kwargs: (_ for _ in ()).throw(OSError("store fault")))
        with pytest.raises(OSError, match="store fault"):
            policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert runtime.store_tool_receipt is real_store
    assert policy.resolve_approval(pending.approval["approval_id"], decision="allow") is None
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert_closed(pending)
    events = runtime.list_runtime_session_events(pending.session, limit=200)
    assert sum(event["event_type"] == "approval_denial_terminal" for event in events) == 1


def test_stale_generation_is_refused_without_changing_pending_runtime_records(tmp_path):
    from core.invocation.ledger import FenceRefused, bump_generation

    pending = pending_write(tmp_path)
    bump_generation(pending.identity["execution_id"], runtime_epoch=pending.identity["runtime_epoch"])
    with pytest.raises(FenceRefused):
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert all(runtime.get_runtime_checkpoint(cid)["status"] == "pending_approval" for cid in pending.checkpoints)
    assert policy.resolve_approval(pending.approval["approval_id"], decision="allow") is None


def test_another_chat_with_the_same_canary_path_is_not_cancelled(tmp_path):
    pending = pending_write(tmp_path)
    foreign = pending_write(tmp_path, task="task-other", session="chat-other")
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert_closed(pending)
    assert runtime.get_runtime_attempt(foreign.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert all(runtime.get_runtime_checkpoint(cid)["status"] == "pending_approval" for cid in foreign.checkpoints)


def test_a_late_preview_cannot_reopen_the_denied_action_receipt(tmp_path):
    pending = pending_write(tmp_path)
    preview = next(event for event in runtime.list_runtime_session_events(pending.session, limit=200)
                   if event["event_type"] == "tool_preview")
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    before = runtime.load_tool_receipt(preview["receipt_id"])
    checkpoint_id = preview["checkpoint_id"]
    details = {key: value for key, value in preview.items()
               if key not in {"session_id", "seq", "event_type", "message", "created_at", "action_record"}}
    action = emit_runtime_event(SimpleNamespace(_runtime_checkpoint_id=lambda ctx: checkpoint_id),
        {"runtime_session_id": pending.session}, event_type="tool_preview", message="Late preview.",
        emit_runtime_event_fn=lambda ctx, **event: runtime.append_runtime_event(session_id=pending.session, **event),
        **details)
    assert action["result"]["status"] == "permission_denied"
    assert runtime.load_tool_receipt(preview["receipt_id"]) == before
    assert runtime.list_recent_runtime_session_events(pending.session, limit=1)[0]["event_type"] == "tool_failed"
    assert_closed(pending)


def test_missing_and_expired_decisions_never_close_pending_work(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    assert policy.resolve_approval("not-an-approval", decision="deny") is None
    monkeypatch.setattr(policy.time, "time", lambda: pending.approval["expires_at"] + 1)
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"


def test_denial_uses_the_owned_approval_link_when_display_receipt_ids_are_scrubbed(tmp_path, monkeypatch):
    from core.agent_runtime import orchestrator
    from core.secret_redaction import redact_secrets

    real_hash = orchestrator._stable_json_hash
    receipt_hashes = iter(("a" * 64, "b" * 64))

    def scrubbed_receipt_hash(value):
        if isinstance(value, dict) and set(value) == {"action_id"}:
            return next(receipt_hashes)
        return real_hash(value)

    monkeypatch.setattr(orchestrator, "_stable_json_hash", scrubbed_receipt_hash)
    pending = pending_write(tmp_path)
    previews = [event for event in runtime.list_runtime_session_events(pending.session, limit=200)
                if event["event_type"] == "tool_preview"]
    assert [event["receipt_id"] for event in previews] == [redact_secrets(value) for value in pending.receipts]
    assert all("[redacted-key]" in event["receipt_id"] for event in previews)
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert_closed(pending)
    preview = previews[0]
    checkpoint_id = preview["checkpoint_id"]
    details = {key: value for key, value in preview.items()
               if key not in {"session_id", "seq", "event_type", "message", "created_at", "action_record"}}
    action = emit_runtime_event(SimpleNamespace(_runtime_checkpoint_id=lambda ctx: checkpoint_id),
        {"runtime_session_id": pending.session}, event_type="tool_preview", message="Late scrubbed preview.",
        emit_runtime_event_fn=lambda ctx, **event: runtime.append_runtime_event(session_id=pending.session, **event),
        **details)
    assert action["result"]["status"] == "permission_denied"
    assert len(runtime.list_runtime_tool_receipts(pending.session)) == 2
    assert_closed(pending)


def test_concurrent_duplicate_denials_have_one_terminal_outcome(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    pending = pending_write(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: policy.resolve_approval(pending.approval["approval_id"], decision="deny"), range(2)))
    assert sum(result is not None for result in results) == 1
    assert_closed(pending)
    events = runtime.list_runtime_session_events(pending.session, limit=200)
    assert sum(event["event_type"] == "approval_denial_terminal" for event in events) == 1


def test_completed_denial_remains_non_resumable_after_permission_state_is_restored(tmp_path):
    from core.invocation.ledger import get_execution

    pending = pending_write(tmp_path)
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    policy.reset_mode_permission_state()
    assert policy.resolve_approval(pending.approval["approval_id"], decision="allow") is None
    assert runtime.latest_resumable_checkpoint(pending.session) is None
    assert get_execution(pending.identity["execution_id"])["state"] == "CANCELLED"
    assert_closed(pending)


def test_an_old_approval_cannot_close_a_receipt_linked_to_a_different_approval(tmp_path):
    pending = pending_write(tmp_path)
    receipt = runtime.load_tool_receipt(pending.receipts[0])
    runtime.store_tool_receipt(receipt_key=receipt["receipt_key"], session_id=receipt["session_id"],
        checkpoint_id=receipt["checkpoint_id"], tool_name=receipt["tool_name"],
        idempotency_key=receipt["idempotency_key"], arguments=receipt["arguments"],
        execution={**receipt["execution"], "approval_id": "a-different-approval"})
    before = runtime.load_tool_receipt(receipt["receipt_key"])
    with pytest.raises(ValueError, match="matching durable receipt"):
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert runtime.load_tool_receipt(receipt["receipt_key"]) == before
    assert runtime.get_runtime_checkpoint(receipt["checkpoint_id"])["status"] == "pending_approval"


@pytest.mark.parametrize("field", ["session_id", "checkpoint_id", "tool_name"])
def test_denied_receipt_cannot_be_returned_to_a_different_owner(tmp_path, field):
    pending = pending_write(tmp_path)
    receipt = runtime.load_tool_receipt(pending.receipts[0])
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    arguments = {key: receipt[key] for key in
                 ("session_id", "checkpoint_id", "tool_name", "idempotency_key", "arguments", "execution")}
    arguments[field] = "a-different-owner"
    with pytest.raises(ValueError, match="ownership mismatch"):
        runtime.store_tool_receipt(receipt_key=receipt["receipt_key"], **arguments)
    assert_closed(pending)
