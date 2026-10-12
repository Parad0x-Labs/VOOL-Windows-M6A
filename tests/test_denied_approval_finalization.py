"""Deny binds typed no-answer truth and the original graph, never an invented reply."""
from __future__ import annotations

import json

import pytest

from core import mode_permission_policy as policy
from core import runtime_continuity as runtime
from core.session_portability.collect import collect_session
from tests.test_denied_approval_lifecycle import (
    assert_closed,
    pending_write,
)
from tests.test_denied_approval_lifecycle import (
    isolated_runtime as isolated_runtime,
)


def rows(sql, params=()):
    from storage.db import get_connection

    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def user_dialogue(pending, *, request_id=None):
    from core.context_namespace import ensure_chat_namespace
    from core.semantic.semantic_admissions import bound_request_context
    from storage.dialogue_memory import record_dialogue_turn

    ensure_chat_namespace(pending.session)
    with bound_request_context(request_id or pending.identity["request_id"]):
        return record_dialogue_turn(pending.session, raw_input="Create the canary.",
            normalized_input="Create the canary.", reconstructed_input="Create the canary.",
            topic_hints=[], reference_targets=[], understanding_confidence=1.0, quality_flags=[])


def test_denial_binds_one_no_answer_referent_to_the_original_request(tmp_path):
    pending = pending_write(tmp_path)
    user_dialogue(pending)
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert_closed(pending)
    final = rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (pending.identity["request_id"],))
    assert len(final) == 1
    assert final[0]["status"] == "no_answer_terminal"
    assert final[0]["terminal_reason"] == "permission_denied"
    assert final[0]["canonical_content"] == final[0]["content_hash"] == ""
    assert final[0]["delivery_status"] == "NOT_ATTEMPTED"
    admission = rows("SELECT * FROM semantic_admissions WHERE sr_id = ?", (final[0]["semantic_result_id"],))
    assert len(admission) == 1 and admission[0]["accepted"] == 1
    assert admission[0]["request_id"] == pending.identity["request_id"]
    assert admission[0]["source_class"] == "refusal_policy"
    assert admission[0]["obligation_set_version"] == pending.obligations["version"]
    assert rows("SELECT * FROM dialogue_turns WHERE speaker_role = 'assistant'") == []
    terminal = [event for event in runtime.list_runtime_session_events(pending.session, limit=200)
                if event["event_type"] == "approval_denial_terminal"]
    assert terminal[0]["finalization_id"] == final[0]["finalization_id"]


def test_signed_export_includes_denied_graph_and_seal_without_a_served_answer(tmp_path):
    from core.session_portability import api

    pending = pending_write(tmp_path)
    dialogue_id = user_dialogue(pending)
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    before = rows("SELECT * FROM a7_finalizations")
    archive = tmp_path / "denied.voolsession"
    receipt = api.export_session(pending.session, archive, passphrase="synthetic-denial-export-only")
    payload = api.load_payload(archive, passphrase="synthetic-denial-export-only")
    assert receipt["counts"]["finalizations"] == 1
    assert receipt["counts"]["obligation_sets"] == 1
    assert payload["turns"] == []
    assert len(payload["dialogue_turns"]) == 1 and payload["dialogue_turns"][0]["turn_id"] == dialogue_id
    assert payload["dialogue_turns"][0]["speaker_role"] == "user"
    final = payload["finalizations"][0]
    assert final["request_id"] == pending.identity["request_id"]
    assert final["status"] == "no_answer_terminal" and final["terminal_reason"] == "permission_denied"
    assert final["content_hash"] == "" and final["delivery_status"] == "NOT_ATTEMPTED"
    graph = payload["obligations"][0]
    assert graph["request_id"] == final["request_id"]
    assert graph["set_id"] == pending.obligations["set_id"]
    assert graph["request_graph"]
    assert all(item["state"] == "cancelled" for item in graph["obligations"])
    assert all(item["evidence_ref"] == pending.approval["approval_id"] for item in graph["obligations"])
    assert before == rows("SELECT * FROM a7_finalizations")
    assert_closed(pending)


def test_repeated_denial_does_not_mint_another_semantic_identity_or_seal(tmp_path):
    pending = pending_write(tmp_path)
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    before = rows("SELECT * FROM a7_finalizations"), rows("SELECT * FROM semantic_admissions")
    assert len(before[0]) == 1
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert before == (rows("SELECT * FROM a7_finalizations"), rows("SELECT * FROM semantic_admissions"))


def test_deny_does_not_consume_or_clear_a_newer_turns_context(tmp_path):
    from core.conductor import obligation_ledger as ledger
    from core.semantic.semantic_admissions import bound_execution_context, bound_request_context, current_request_id
    from core.semantic.semantic_result_seam import admit_semantic_result, current_admission, reset_admission

    denied = pending_write(tmp_path)
    newer = pending_write(tmp_path, task="task-newer")
    reset_admission()
    with bound_request_context(newer.identity["request_id"]), bound_execution_context(newer.identity):
        token = ledger.bind_active_set(newer.obligations["set_id"], newer.obligations["version"])
        try:
            admit_semantic_result({"response": "Newer answer."}, turn_id="task-newer")
            admission = current_admission()
            policy.resolve_approval(denied.approval["approval_id"], decision="deny")
            assert current_admission() is admission
            assert current_request_id() == newer.identity["request_id"]
            assert ledger.active_set() == (newer.obligations["set_id"], newer.obligations["version"])
        finally:
            ledger._ACTIVE_SET.reset(token)
            reset_admission()
    assert len(rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (denied.identity["request_id"],))) == 1
    assert rows("SELECT * FROM a7_finalizations WHERE request_id = ?", (newer.identity["request_id"],)) == []
    assert runtime.get_runtime_attempt(newer.attempt_id)["lifecycle_state"] == "WAITING_APPROVAL"


def test_a_foreign_dialogue_request_claim_exports_neither_seal_nor_graph(tmp_path):
    own = pending_write(tmp_path)
    foreign = pending_write(tmp_path, session="chat-other", task="task-other")
    user_dialogue(own, request_id=foreign.identity["request_id"])
    policy.resolve_approval(foreign.approval["approval_id"], decision="deny")
    assert len(rows("SELECT * FROM a7_finalizations")) == 1
    payload, _, counts, _ = collect_session(own.session)
    assert payload["finalizations"] == payload["obligations"] == []
    assert counts.finalizations == counts.obligation_sets == 0


def test_stale_denial_does_not_admit_or_finalize_no_answer(tmp_path):
    from core.invocation.ledger import FenceRefused, bump_generation

    pending = pending_write(tmp_path)
    bump_generation(pending.identity["execution_id"], runtime_epoch=pending.identity["runtime_epoch"])
    with pytest.raises(FenceRefused):
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert rows("SELECT * FROM a7_finalizations") == rows("SELECT * FROM semantic_admissions") == []
    snapshot = rows("SELECT snapshot_json FROM obligation_sets WHERE set_id = ?", (pending.obligations["set_id"],))
    assert all(item["state"] == "planned" for item in json.loads(snapshot[0]["snapshot_json"])["obligations"])


def test_interruption_after_the_seal_reuses_it_without_another_admission(tmp_path, monkeypatch):
    pending = pending_write(tmp_path)
    real_append = runtime.append_runtime_event

    def fail_terminal(**kwargs):
        if kwargs.get("event_type") == "approval_denial_terminal":
            raise OSError("terminal event fault")
        return real_append(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "append_runtime_event", fail_terminal)
        with pytest.raises(OSError, match="terminal event fault"):
            policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    before = rows("SELECT * FROM a7_finalizations"), rows("SELECT * FROM semantic_admissions")
    assert len(before[0]) == len(before[1]) == 1
    assert policy.resolve_approval(pending.approval["approval_id"], decision="deny") is None
    assert before == (rows("SELECT * FROM a7_finalizations"), rows("SELECT * FROM semantic_admissions"))
    assert_closed(pending)


def test_a_newer_set_under_the_same_request_does_not_replace_the_denied_graph(tmp_path):
    from core.conductor import obligation_ledger as ledger

    pending = pending_write(tmp_path)
    user_dialogue(pending)
    newer = ledger.open_obligation_set(request_id=pending.identity["request_id"],
        request_text="A different graph version.",
        obligations=[{"obligation_id": "newer-demand", "kind": "demand", "text": "Other work."}])
    policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    payload, _, _, _ = collect_session(pending.session)
    assert payload["obligations"][0]["set_id"] == pending.obligations["set_id"]
    assert payload["obligations"][0]["version"] == pending.obligations["version"]
    assert ledger.closure_verdict(newer["set_id"], newer["version"])["open_count"] == 1


def test_a_missing_original_set_refuses_a_seal_instead_of_fabricating_coverage(tmp_path):
    from storage.db import get_connection

    pending = pending_write(tmp_path)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM obligation_sets WHERE set_id = ?", (pending.obligations["set_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="obligation set belongs"):
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    assert rows("SELECT * FROM a7_finalizations") == rows("SELECT * FROM semantic_admissions") == []
    assert_closed(pending)


def test_no_answer_refuses_an_admission_from_another_request(tmp_path):
    from core.finalization import FinalizationRejected, no_answer_terminal
    from core.semantic.semantic_admissions import bound_execution_context, bound_request_context
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    own = pending_write(tmp_path)
    foreign = pending_write(tmp_path, task="task-foreign", session="chat-foreign")
    reset_admission()
    try:
        with bound_request_context(foreign.identity["request_id"]):
            admit_semantic_result({"response": "Foreign admitted bytes."}, turn_id="task-foreign")
        with bound_request_context(own.identity["request_id"]), bound_execution_context(own.identity):
            with pytest.raises(FinalizationRejected, match="ADMISSION_REQUEST_MISMATCH"):
                no_answer_terminal(turn_id="task-denied", reason_code="permission_denied",
                    closure={"covered": True, "open_count": 0, "set_version": "test"})
        assert rows("SELECT * FROM a7_finalizations") == []
    finally:
        reset_admission()


@pytest.mark.parametrize("typed,policy_source", [(False, True), (True, False), (False, False)])
def test_untyped_or_non_policy_empty_content_never_gets_an_admission(typed, policy_source):
    from core.semantic.semantic_result_seam import (
        SemanticSource,
        admit_semantic_result,
        current_admission,
        reset_admission,
    )

    reset_admission()
    result = {"response": "", "reason_code": "permission_denied"}
    if typed:
        result["type"] = "no_answer.terminal"
    admit_semantic_result(result, source_override=SemanticSource.REFUSAL_POLICY if policy_source else SemanticSource.MODEL)
    assert current_admission() is None
    assert rows("SELECT * FROM semantic_admissions") == []


def test_denied_finalization_bounds_refuse_without_truncation(tmp_path, monkeypatch):
    from core.session_portability import api, schema
    from core.session_portability.collect import OverBound

    for task in ("task-1", "task-2"):
        pending = pending_write(tmp_path, task=task)
        user_dialogue(pending)
        policy.resolve_approval(pending.approval["approval_id"], decision="deny")
    monkeypatch.setitem(schema.BOUNDS, "finalizations", 1)
    with pytest.raises(OverBound, match="finalizations"):
        collect_session(pending.session)
    with pytest.raises(api.PortabilityRefused) as error:
        api.preview_export(pending.session)
    assert error.value.code == "BUNDLE_LIMIT_EXCEEDED"
