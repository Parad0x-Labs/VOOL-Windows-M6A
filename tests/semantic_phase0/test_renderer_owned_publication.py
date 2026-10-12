"""Closure-row ownership reaches finalization without certifying model prose."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from core import finalization
from core.conductor import obligation_ledger as ol
from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission
from tests.semantic_phase0.test_graph_carried_through_runtime import TEXT, _events, _mint
from tests.semantic_phase0.test_graph_carried_through_runtime import fresh_store as fresh_store


def _partial_turn():
    opened, graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    assert ol.record_slice_dispatch(sid, ver, unit_id="u1", subtask_id="oslo", state="succeeded")
    assert ol.record_slice_consumption(sid, ver, unit_id="u1", evidence="slice_answer_record")
    return {"set_id": sid, "set_version": ver}, graph


def _finalize(content, closure, *, context=None):
    reset_admission()
    try:
        admitted = admit_semantic_result({"response": content})
        semantic_id = admitted["_semantic_admission"]["semantic_result_id"]
        commit = finalization.finalize_answer(
            turn_id="turn-1", canonical_content=content, closure=closure,
            source_context=context or {"session_id": "owned-render", "runtime_session_id": "owned-render"},
        )
        stored = finalization.get_finalization_by_semantic_id(semantic_id)
        events = [event for event in _events("owned-render") if event.get("event_type") == finalization.CONSERVATION_EVENT]
        assert len(events) == 1
        assert commit["content_hash"] == stored["content_hash"]
        assert stored["content_hash"] == "sha256:" + hashlib.sha256(stored["canonical_content"].encode()).hexdigest()
        return commit, stored, events[0]["verdict"]
    finally:
        reset_admission()


@pytest.mark.parametrize("enforced", [False, True])
def test_actual_finalization_records_owned_disclosure_and_unchanged_answer_bytes(enforced, monkeypatch, fresh_store):
    if enforced:
        monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    closure, graph = _partial_turn()
    content = "🌙 Oslo weather: 12 C, overcast."
    _commit, stored, verdict = _finalize(content, closure)
    expected = content + "\n\nCould not be answered:\n* and how did tesla close — not dispatched"
    assert stored["canonical_content"] == expected
    assert verdict["publication_conservation"]["conserved"] is True
    rows = {row["slot_id"]: row for row in verdict["slot_evidence"]}
    assert rows["u1"]["visibility_basis"] == "lexical_inference"
    assert rows["u2"]["visibility_basis"] == "renderer_owned"
    assert rows["u2"]["disposition"] == "unknown"
    assert rows["u2"]["ledger_state"] == "unanswered"
    assert rows["u2"]["final_byte_coverage"] == "verified"
    assert rows["u2"]["turn_id"] == graph.turn_id
    payload = expected.encode()
    assert payload[rows["u2"]["byte_start"]:rows["u2"]["byte_end"]] == "* and how did tesla close — not dispatched".encode()
    assert "Oslo" not in json.dumps(verdict) and "tesla" not in json.dumps(verdict)


def test_caller_context_cannot_claim_renderer_ownership(fresh_store):
    closure, _graph = _partial_turn()
    content = "Oslo weather: 12 C.\n\nCould not be answered:\n* how did tesla close — not dispatched"
    context = {"session_id": "owned-render", "runtime_session_id": "owned-render", "renderer_owned": {"u2": content}}
    _commit, stored, verdict = _finalize(content, closure, context=context)
    assert stored["canonical_content"] == content
    assert all(row["visibility_basis"] == "lexical_inference" for row in verdict["slot_evidence"])


def test_table_normalization_rebinds_owned_suffix_to_exact_final_bytes(fresh_store):
    from core.presentation.answer_render import normalize_structured_shape

    closure, _graph = _partial_turn()
    content = "🌙 Oslo weather: 12 C.\n\n|Place|Reading|\n|---|---|\n|Oslo|12 C|"
    appended = content + "\n\nCould not be answered:\n* and how did tesla close — not dispatched"
    expected, control = normalize_structured_shape(appended, {})
    assert control["normalized"] == ["table"] and expected != appended
    commit, stored, verdict = _finalize(content, closure)
    assert stored["canonical_content"] == expected
    assert commit["display_metadata"]["render_lane"]["normalized"] == ["table"]
    assert verdict["final_content_hash"] == commit["content_hash"]
    assert verdict["publication_conservation"]["conserved"] is True
    row = next(row for row in verdict["slot_evidence"] if row["slot_id"] == "u2")
    assert row["visibility_basis"] == "renderer_owned" and row["final_byte_coverage"] == "verified"
    assert expected.encode()[row["byte_start"]:row["byte_end"]] == "* and how did tesla close — not dispatched".encode()


@pytest.mark.parametrize("damage", ["foreign_turn", "foreign_graph", "foreign_set", "stale_version", "wrong_state", "bad_span", "wrong_span", "foreign_slot", "duplicate"])
def test_corrupted_owned_receipt_refuses_durable_finalization(damage, monkeypatch, fresh_store):
    closure, _graph = _partial_turn()
    monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    real_sweep = finalization._rss_closure_sweep

    def corrupt(*args, **kwargs):
        content, census = real_sweep(*args, **kwargs)
        sink = kwargs["_render_receipts"]
        assert len(sink) == 1 and len(sink[0].rows) == 1
        receipt = sink[0]
        if damage == "duplicate":
            sink.append(receipt)
        elif damage in {"wrong_state", "bad_span", "wrong_span", "foreign_slot"}:
            row = receipt.rows[0]
            change = {"ledger_state": "satisfied"} if damage == "wrong_state" else (
                {"byte_start": -1} if damage == "bad_span" else {"slot_id": "foreign"}
            )
            if damage == "wrong_span":
                change = {"byte_start": 0, "byte_end": len(b"Could not be answered:")}
            sink[0] = replace(receipt, rows=(replace(row, **change),))
        else:
            field = {"foreign_turn": "turn_id", "foreign_graph": "graph_digest", "foreign_set": "set_id", "stale_version": "set_version"}[damage]
            sink[0] = replace(receipt, **{field: "foreign"})
        return content, census

    # Fault injection preserves the real sweep, graph, ledger, renderer, verifier and DB.
    monkeypatch.setattr(finalization, "_rss_closure_sweep", corrupt)
    reset_admission()
    try:
        admission = admit_semantic_result({"response": "Oslo weather: 12 C."})
        sid = admission["_semantic_admission"]["semantic_result_id"]
        with pytest.raises(finalization.FinalizationRejected, match="SLOT_CONSERVATION:publication"):
            finalization.finalize_answer(
                turn_id="turn-1", canonical_content="Oslo weather: 12 C.", closure=closure,
                source_context={"session_id": "owned-render", "runtime_session_id": "owned-render"},
            )
        assert finalization.get_finalization_by_semantic_id(sid) is None
        (event,) = [event for event in _events("owned-render") if event.get("event_type") == finalization.CONSERVATION_EVENT]
        verdict = event["verdict"]
        assert verdict["publication_conservation"]["conserved"] is False
        if damage == "duplicate":
            assert verdict["publication_conservation"]["duplicated"] == ["u2"]
        else:
            assert verdict["publication_conservation"]["invalid"]
    finally:
        reset_admission()


@pytest.mark.parametrize("damage", ["lost_section", "edited_row", "lost_header"])
def test_post_render_damage_cannot_escape_through_lexical_fallback(damage, monkeypatch, fresh_store):
    from core.presentation import answer_render

    closure, _graph = _partial_turn()
    real_normalize = answer_render.normalize_structured_shape

    def damage_render(content, context):
        text, record = real_normalize(content, context)
        if damage == "lost_section":
            return text.split("\n\nCould not be answered:")[0], record
        if damage == "lost_header":
            return text.replace("Could not be answered:\n", ""), record
        return text.replace("not dispatched", "not dispatched (edited)"), record

    monkeypatch.setattr(answer_render, "normalize_structured_shape", damage_render)
    _commit, _stored, verdict = _finalize("Oslo weather: 12 C.", closure)
    assert verdict["publication_conservation"]["conserved"] is False
    assert verdict["publication_conservation"]["invalid"] == ["u2"]
    row = next(row for row in verdict["slot_evidence"] if row["slot_id"] == "u2")
    assert row["visibility_basis"] == "renderer_owned" and row["final_byte_coverage"] == "indeterminate"


def test_cancellation_invalidates_owned_receipt_after_all_rendering(fresh_store):
    closure, _graph = _partial_turn()
    marker = threading.Event()
    marker.set()
    context = {"session_id": "owned-render", "runtime_session_id": "owned-render", "cancel_event": marker}
    commit, stored, verdict = _finalize("Oslo weather: 12 C.", closure, context=context)
    assert stored["canonical_content"] == finalization.CANCELLED_BEFORE_PUBLICATION
    assert verdict["final_content_hash"] == commit["content_hash"]
    assert verdict["publication_conservation"]["conserved"] is False
    assert verdict["publication_conservation"]["invalid"] == ["u2"]
    assert all(row["final_byte_coverage"] == "indeterminate" for row in verdict["slot_evidence"])


def test_literal_lane_keeps_exact_bytes_and_emits_no_owned_disclosure(fresh_store):
    closure, _graph = _partial_turn()
    closure["literal_answer_lane"] = True
    _commit, stored, verdict = _finalize("324", closure)
    assert stored["canonical_content"] == "324"
    assert all(row["visibility_basis"] == "lexical_inference" for row in verdict["slot_evidence"])


def test_owned_partial_and_unknown_rows_do_not_promote_fulfillment(fresh_store):
    closure, _graph = _partial_turn()
    receipts = []
    content, census = finalization._rss_closure_sweep(
        "The remaining figures are not independently verified.", closure, ("removed claim",),
        published_in_part=True, _render_receipts=receipts,
    )
    assert "Answered in part:" in content and "Could not be answered:" in content
    assert census["demand_indeterminate"] == 1 and census["demand_unanswered"] == 1
    assert len(receipts) == 1
    with pytest.raises(FrozenInstanceError):
        receipts[0].section = "forged"
    verdict = finalization._semantic_conservation_verdict(closure, content, {}, _render_receipts=tuple(receipts))
    assert verdict["publication_conservation"]["conserved"] is True
    rows = {row["slot_id"]: row for row in verdict["slot_evidence"]}
    assert rows["u1"]["disposition"] == "unverified" and rows["u2"]["disposition"] == "unknown"
    assert rows["u1"]["fulfillment_evidence"] == "indeterminate"
    assert all(row["visibility_basis"] == "renderer_owned" and row["final_byte_coverage"] == "verified" for row in rows.values())


def test_renderer_offsets_keep_duplicate_text_rows_separate():
    owned = []
    pending = [
        {"unit_id": sid, "state": "unanswered", "text": "same 🌙", "reason": "not dispatched"}
        for sid in ("u1", "u2")
    ]
    section = finalization._render_closure_rows(pending, "body", _owned_rows=owned)
    payload = section.encode()
    assert len(owned) == 2 and owned[0].byte_end < owned[1].byte_start
    for row in owned:
        assert payload[row.byte_start:row.byte_end] == "* same 🌙 — not dispatched".encode()


def test_single_model_story_remains_unverified_without_owned_answer_evidence(fresh_store):
    text = "Write a four-sentence original story about a lighthouse keeper who discovers a clock running backwards."
    opened, _graph = _mint(text)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    closure = {"set_id": sid, "set_version": ver}
    content = "Elias opened the brass door. The hands ticked in reverse. Sunset became dawn. The mechanism locked."
    _commit, stored, verdict = _finalize(content, closure)
    assert stored["canonical_content"] == content
    (row,) = verdict["slot_evidence"]
    assert row["disposition"] == "unverified" and row["visibility_basis"] == "lexical_inference"
    assert row["final_byte_coverage"] == "indeterminate"
    assert verdict["publication_conservation"]["conserved"] is False
