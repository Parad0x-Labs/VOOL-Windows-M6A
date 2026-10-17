"""KAS-049: a pending preview cannot occupy the denied request's immutable finality."""
from __future__ import annotations

import json
from contextvars import copy_context

import pytest

from core import runtime_continuity as runtime
from core.conductor import obligation_ledger as ledger
from core.semantic.semantic_admissions import bound_execution_context, bound_request_context
from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission
from core.web.api.runtime import (
    RuntimeServices,
    _final_task_event,
    _response_commit,
    _stream_agent_with_events_inner,
    ollama_chat_response,
    ollama_stream_chunks,
    openai_chat_response,
)
from tests.test_approval_resolution_transport import resolve
from tests.test_denied_approval_finalization import rows
from tests.test_denied_approval_lifecycle import assert_closed, pending_write
from tests.test_denied_approval_lifecycle import isolated_runtime as isolated_runtime

REQUEST = (
    "Create a plain text file named m6b-denied-owner-canary-20261012-d.txt in the active project "
    "folder with exactly this content: M6B_SYNTHETIC_CANARY. Use the workspace file tool, "
    "not a terminal command. Do not modify any other file."
)


def pending_preview(pending):
    return {
        "response": "Manual mode requires approval for this exact action. Review the details, then allow or deny this request.",
        "mode": "tool_preview", "task_outcome": "pending_approval",
        "response_class": "approval_required", "success": False,
        "route_reason": "demand_owned_mixed_turn_pending_approval",
        "details": {"approval_request": pending.approval},
        "_execution_identity": pending.identity,
    }


def frame_pending(pending, formatter=None):
    with bound_request_context(pending.identity["request_id"]), bound_execution_context(pending.identity):
        ledger.bind_active_set(pending.obligations["set_id"], pending.obligations["version"])
        reset_admission()
        result = pending_preview(pending)
        admit_semantic_result(result, turn_id=pending.approval["task_id"])
        # This is the existing served-byte disposition before the HTTP framing seam.
        ledger.record_disposition(pending.obligations["set_id"], pending.obligations["version"],
            f"ob:{pending.attempt_id}:answer", "satisfied", evidence_source="served_bytes")
        result["_closure_verdict"] = ledger.closure_verdict(pending.obligations["set_id"], pending.obligations["version"])
        context = {"runtime_session_id": pending.session,
            "cancel_turn_id": pending.approval["task_id"], "_execution_identity": pending.identity}
        return (formatter or _response_commit)(result, source_context=context)


def test_installed_preview_then_one_deny_finalizes_original_request(tmp_path):
    pending = pending_write(tmp_path, request=REQUEST, prose_obligation=True)
    copy_context().run(frame_pending, pending)
    status, body = resolve(pending)
    if status != 200:
        diagnostic = next(event for event in runtime.list_runtime_session_events(pending.session, limit=200)
            if event["event_type"] == "approval_denial_finalization_failed")
        assert diagnostic["exception_class"] == "FinalizationRejected"
        assert diagnostic["failure_reason"] == "DENIAL_FINALIZATION_CONFLICT"
        assert diagnostic["exception_message"] == (
            "finalized response already exists with different content "
            "(parent_task_id='' stored=DENIAL_FINALIZATION_CONFLICT attempted=)")
        assert diagnostic["request_id"] == pending.identity["request_id"]
    assert status == 200 and body["ok"] is True, json.dumps(body)
    assert_closed(pending)
    final = rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (pending.identity["request_id"],))
    assert len(final) == 1 and final[0]["status"] == "no_answer_terminal"
    assert final[0]["terminal_reason"] == "permission_denied"
    assert final[0]["canonical_content"] == final[0]["content_hash"] == ""


@pytest.mark.parametrize("door", ["commit", "ollama", "openai", "chunks"])
def test_permission_pause_frames_no_answer_bytes_and_leaves_demand_open(tmp_path, door):
    pending = pending_write(tmp_path, request=REQUEST, prose_obligation=True)

    def format_result(result, *, source_context):
        if door == "ollama":
            return ollama_chat_response(result, "synthetic-local", RuntimeServices(), source_context)
        if door == "openai":
            return openai_chat_response(result, "synthetic-local", source_context)
        if door == "chunks":
            return [json.loads(chunk) for chunk in ollama_stream_chunks(result, "synthetic-local")]
        return _response_commit(result, source_context=source_context)

    framed = copy_context().run(frame_pending, pending, format_result)
    if door == "ollama":
        assert framed["message"]["content"] == "" and framed["vool_response_commit"] == {}
    elif door == "openai":
        assert framed["choices"][0]["message"]["content"] == "" and framed["vool_response_commit"] == {}
    elif door == "chunks":
        assert len(framed) == 1 and framed[0]["done"] is True
        assert framed[0]["message"]["content"] == "" and "vool_response_commit" not in framed[0]
    else:
        assert framed == {}
    assert rows("SELECT * FROM a7_finalizations") == []
    snapshot = ledger._read_snapshot(pending.obligations["set_id"], pending.obligations["version"])
    assert all(item["state"] == "planned" for item in snapshot["obligations"] if item["kind"] == "demand")
    assert runtime.get_runtime_attempt(pending.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"
    assert resolve(pending)[0] == 200
    assert_closed(pending)


def test_real_stream_worker_keeps_the_permission_pause_nonterminal(tmp_path):
    pending = pending_write(tmp_path, request=REQUEST, prose_obligation=True)
    frames = [json.loads(chunk) for chunk in _stream_agent_with_events_inner(
        RuntimeServices(), REQUEST, session_id=pending.session,
        source_context={"cancel_turn_id": pending.approval["task_id"]}, model="synthetic-local",
        emit_task_events=True, run_agent_provider=lambda *args, **kwargs: pending_preview(pending),
    )]
    assert all(not frame.get("message", {}).get("content") for frame in frames)
    assert all("vool_terminal" not in frame and "vool_response_commit" not in frame for frame in frames)
    assert frames[-1]["vool_event"]["type"] == "permission.required"
    assert rows("SELECT * FROM a7_finalizations") == []
    assert resolve(pending)[0] == 200
    assert_closed(pending)


def test_pending_result_is_not_a_completed_task():
    event = _final_task_event({"task_outcome": "pending_approval", "tool_name": "workspace.write_file"})
    assert event["type"] == "permission.required" and event["status"] == "pending_approval"


def test_pending_result_cannot_hide_an_attached_immutable_answer():
    with pytest.raises(RuntimeError, match="pending approval cannot carry"):
        _response_commit({"task_outcome": "pending_approval", "vool_response_commit": {
            "type": "response.commit", "canonical_content": "Existing truth.",
        }}, source_context=None)


def test_entrance_does_not_finalize_a_permission_notice(tmp_path):
    from core.agent_runtime.agent import _certify_entrance_turn

    pending = pending_write(tmp_path, request=REQUEST, prose_obligation=True)
    result = pending_preview(pending)
    copy_context().run(_certify_entrance_turn, {"result": result}, pending.obligations)
    assert "vool_response_commit" not in result
    assert rows("SELECT * FROM a7_finalizations") == []
    assert resolve(pending)[0] == 200
    assert_closed(pending)


def test_a_real_conflicting_answer_remains_immutable_and_denial_fails_closed(tmp_path):
    from core.finalization import finalize_answer

    pending = pending_write(tmp_path, request=REQUEST, prose_obligation=True)

    def seal_answer():
        with bound_request_context(pending.identity["request_id"]), bound_execution_context(pending.identity):
            ledger.bind_active_set(pending.obligations["set_id"], pending.obligations["version"])
            reset_admission()
            admit_semantic_result({"response": "Existing immutable answer."}, turn_id=pending.approval["task_id"])
            ledger.record_disposition(pending.obligations["set_id"], pending.obligations["version"],
                f"ob:{pending.attempt_id}:answer", "satisfied", evidence_source="served_bytes")
            return finalize_answer(turn_id=pending.approval["task_id"], canonical_content="Existing immutable answer.")

    copy_context().run(seal_answer)
    before = rows("SELECT * FROM a7_finalizations")
    status, body = resolve(pending)
    assert status == 500 and body["fault_code"] == "internal"
    assert rows("SELECT * FROM a7_finalizations") == before
    assert not any(event["event_type"] == "approval_denial_terminal"
        for event in runtime.list_runtime_session_events(pending.session, limit=200))
    assert_closed(pending)
