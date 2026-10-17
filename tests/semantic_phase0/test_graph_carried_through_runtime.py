"""The RequestGraph is carried through the runtime: minted at the door with the ledger's own ids,
persisted in the obligation set, read back by the sweep, reported on the receipt, and reconciled
into slot + publication conservation verdicts (observe-only unless the operator enforces).

Deterministic environmental assertions: what the ledger holds, what the receipt says, what the
verdict event records. No expected prose, no model, no network.
"""
from __future__ import annotations

import json

import pytest

from core.agent_runtime.answer_coverage import demand_units
from core.conductor import obligation_ledger as ol
from core.semantic import reach as semantic_reach
from core.semantic.bridges.ledger import (
    SOURCE_GRAPH,
    SOURCE_LEGACY_REPARSE,
    demand_obligation_rows,
    frozen_slot_ids,
    snapshot_graph_payload,
)
from core.semantic.producers.lexical import PRODUCER_NAME, PRODUCER_VERSION, lexical_request_graph
from core.semantic.receipt import (
    ADMISSION_NOT_ATTEMPTED_REASON,
    RESOLUTION_RECEIPT_EVENT,
    RESOLVER_NOT_ATTEMPTED_REASON,
    SLOT_ATTEMPTED,
    SLOT_NOT_ATTEMPTED,
)
from core.semantic.turn_graph import (
    REQUEST_GRAPH_KEY,
    current_graph_projection,
    failed_projection,
    graph_projection,
    publish_request_graph,
)
from core.semantic.turn_observation import _write_receipt
from storage import db as sdb

# Imported rather than discovered -- see `_fixtures` for why this is not a conftest.
from tests.semantic_phase0._fixtures import (
    block_outbound_network,
    keep_the_checkout_clean,
    make_agent_module,
    pin_the_signing_key_passphrase,
    pin_volatile_machine_observations,
    reseal_network_after_function_fixtures,
)

TEXT = "what's the weather in oslo and how did tesla close"


@pytest.fixture
def fresh_store(tmp_path):
    from core.runtime_continuity import configure_runtime_continuity_db_path
    from storage.db import active_default_db_path
    from storage.migrations import run_migrations

    sdb.configure_default_db_path(tmp_path / "keystone.db")
    run_migrations()
    configure_runtime_continuity_db_path(active_default_db_path())
    yield
    sdb.configure_default_db_path(None)
    ol.clear_active_set()


def _mint(text: str, *, attempt: str = "att-1", with_graph: bool = True) -> tuple[dict, object]:
    units = demand_units(text)
    graph = lexical_request_graph(text, turn_id="turn-1")
    rows = demand_obligation_rows(graph, attempt_id=attempt, request_text=text, units={u.unit_id: u for u in units})
    opened = ol.open_obligation_set(
        request_text=text, request_id="req-1", obligations=rows,
        request_graph=snapshot_graph_payload(graph) if with_graph else None,
    )
    return opened, graph


def _events(session_id: str) -> list[dict]:
    from core.runtime_continuity import list_runtime_session_events

    return list(list_runtime_session_events(session_id, limit=400))


# -- ledger: the graph is persisted with the set and read back ------------------------------


def test_the_set_snapshot_carries_the_graph_and_the_sweep_reads_ids_from_it(fresh_store) -> None:
    opened, graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    stored = ol.snapshot_request_graph(sid, ver)
    assert stored is not None and stored["request_text"] == TEXT
    ids, source = frozen_slot_ids(stored)
    assert source == SOURCE_GRAPH
    assert ids == {str(s) for s in graph.slot_ids} == {u.unit_id for u in demand_units(TEXT)}
    assert {row["unit_id"] for row in ol.demand_obligations(sid, ver)} == ids


def test_a_pre_graph_set_is_served_by_the_legacy_reparse_and_says_so(fresh_store) -> None:
    opened, _graph = _mint(TEXT, with_graph=False)
    stored = ol.snapshot_request_graph(*(opened["set_id"], opened["version"]))
    assert stored is not None and stored["request_graph"] is None
    ids, source = frozen_slot_ids(stored)
    assert source == SOURCE_LEGACY_REPARSE and ids == {u.unit_id for u in demand_units(TEXT)}


# -- receipt: shape, graph and admission slots are filled from the turn ------------------------


def test_receipt_reports_the_graph_shape_and_no_stale_phase_claim(monkeypatch, fresh_store) -> None:
    monkeypatch.setenv("VOOL_SEMANTIC_REACH", "1")
    graph = lexical_request_graph(TEXT, turn_id="turn-r")
    context = {"session_id": "keystone-receipt", "runtime_session_id": "keystone-receipt", "cancel_turn_id": "turn-r"}
    publish_request_graph(context, graph_projection(graph, producer=PRODUCER_NAME, producer_version=PRODUCER_VERSION))
    assert current_graph_projection(context) is not None
    with semantic_reach.observing_turn(session_id="keystone-receipt", turn_id="turn-r") as recorder:
        assert recorder is not None
        _write_receipt(recorder, context)
    receipts = [json.loads(e["receipt"]) if isinstance(e.get("receipt"), str) else e.get("receipt")
                for e in _events("keystone-receipt") if e.get("event_type") == RESOLUTION_RECEIPT_EVENT]
    assert receipts, "no receipt was written"
    receipt = receipts[-1]
    assert receipt["shape"] == "multi_clause"
    semantic = receipt["semantic"]
    assert semantic["state"] == SLOT_NOT_ATTEMPTED and semantic["detail"] == RESOLVER_NOT_ATTEMPTED_REASON
    assert semantic["graph"]["producer"] == PRODUCER_NAME and semantic["graph"]["slot_ids"] == ["u1", "u2"]
    assert "phase_0" not in json.dumps(receipt)
    assert receipt["admission"]["state"] == SLOT_NOT_ATTEMPTED
    assert receipt["admission"]["detail"] == ADMISSION_NOT_ATTEMPTED_REASON
    # The projection is text-free: none of the user's words reach the receipt through it.
    for word in ("oslo", "tesla"):
        assert word not in json.dumps(semantic["graph"]).lower()


def test_a_failed_producer_is_visible_on_the_receipt_not_silent(monkeypatch, fresh_store) -> None:
    monkeypatch.setenv("VOOL_SEMANTIC_REACH", "1")
    context = {"session_id": "keystone-failed", "runtime_session_id": "keystone-failed", "cancel_turn_id": "turn-f"}
    publish_request_graph(context, failed_projection("RuntimeError", turn_id="turn-f"))
    with semantic_reach.observing_turn(session_id="keystone-failed", turn_id="turn-f") as recorder:
        _write_receipt(recorder, context)
    receipt = [json.loads(e["receipt"]) if isinstance(e.get("receipt"), str) else e.get("receipt")
               for e in _events("keystone-failed") if e.get("event_type") == RESOLUTION_RECEIPT_EVENT][-1]
    assert receipt["shape"] == "unknown"
    assert "graph producer failed: RuntimeError" in receipt["semantic"]["detail"]


def test_an_admitted_semantic_result_fills_the_admission_slot(monkeypatch, fresh_store) -> None:
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    monkeypatch.setenv("VOOL_SEMANTIC_REACH", "1")
    reset_admission()
    admit_semantic_result({"response": "Oslo: 12 C", "route_reason": "test_lane"})
    context = {"session_id": "keystone-adm", "runtime_session_id": "keystone-adm", "cancel_turn_id": "turn-a"}
    try:
        with semantic_reach.observing_turn(session_id="keystone-adm", turn_id="turn-a") as recorder:
            _write_receipt(recorder, context)
    finally:
        reset_admission()
    receipt = [json.loads(e["receipt"]) if isinstance(e.get("receipt"), str) else e.get("receipt")
               for e in _events("keystone-adm") if e.get("event_type") == RESOLUTION_RECEIPT_EVENT][-1]
    assert receipt["admission"]["state"] == SLOT_ATTEMPTED
    assert receipt["admission"]["admitted_count"] == 1 and receipt["admission"]["results"][0]["accepted"] is True


# -- finalization: conservation verdicts, observe-only, enforceable -------------------------------


def _sweep_to(sid: str, ver: str, states: dict[str, str]) -> None:
    for row in ol.demand_obligations(sid, ver):
        state = states.get(str(row["unit_id"]))
        if state:
            assert ol.record_disposition(sid, ver, row["obligation_id"], state, evidence_source=ol.RSS_SWEEP_EVIDENCE)


def test_conservation_verdict_is_recorded_and_tells_the_truth(fresh_store) -> None:
    from core.agent_runtime.answer_coverage import DEMAND_SATISFIED, unit_answer_evidence
    from core.finalization import CONSERVATION_EVENT, _semantic_conservation_verdict

    opened, _graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    _sweep_to(sid, ver, {"u1": "satisfied", "u2": "unanswered"})
    context = {"session_id": "keystone-verdict", "runtime_session_id": "keystone-verdict"}
    answered_bytes = "Oslo weather: 12 C, overcast."
    assert unit_answer_evidence(TEXT, answered_bytes).get("u1") == DEMAND_SATISFIED, "fixture: the bytes must answer u1"
    content = answered_bytes + "\n\nCould not be answered:\n- how did tesla close (not dispatched)"
    verdict = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, content, context)
    assert verdict is not None and verdict["status"] == "evaluated"
    assert verdict["evidence_grade"] == "text_ladder_over_final_bytes"
    assert verdict["frozen_slot_count"] == 2 and verdict["terminal_slot_count"] == 2
    assert verdict["slot_conservation"]["conserved"] is True
    assert verdict["publication_conservation"]["conserved"] is True
    assert verdict["enforced"] is False
    events = [e for e in _events("keystone-verdict") if e.get("event_type") == CONSERVATION_EVENT]
    assert events, "the verdict must land on the ledger as an event"

    # A hidden slot: the unanswered unit is NOT disclosed in the bytes -> publication not conserved.
    hidden = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, answered_bytes, context)
    assert hidden is not None and hidden["publication_conservation"]["conserved"] is False
    assert hidden["publication_conservation"]["hidden"] == ["u2"]

    # The audit's probe: a ledger that says satisfied over bytes that do not answer -> NOT conserved.
    unrelated = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, "Completely unrelated text.", context)
    assert unrelated is not None and unrelated["status"] == "evaluated"
    assert unrelated["slot_conservation"]["conserved"] is False or unrelated["publication_conservation"]["conserved"] is False
    assert "u1" in unrelated["publication_conservation"]["hidden"]


def test_verdict_binds_actual_final_bytes_and_does_not_persist_answer_text(fresh_store) -> None:
    import hashlib

    from core.finalization import _semantic_conservation_verdict
    from core.semantic.graph_serialization import graph_digest

    opened, graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    _sweep_to(sid, ver, {"u1": "satisfied", "u2": "unanswered"})
    content = "🌙 Oslo weather: 12 C, overcast.\r\n\r\nCould not be answered:\r\n- how did tesla close (not dispatched)"
    verdict = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, content, {"session_id": "bound-bytes"})
    assert verdict["publication_conservation"]["conserved"] is True
    assert verdict["publication_conservation"]["invalid"] == []
    assert verdict["publication_evidence_grade"] == "slot_turn_graph_bound_final_byte_spans"
    payload = content.encode("utf-8")
    expected_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert verdict["final_content_hash"] == expected_hash
    by_slot = {row["slot_id"]: row for row in verdict["slot_evidence"]}
    for row in by_slot.values():
        assert row["turn_id"] == "turn-1" and row["graph_digest"] == graph_digest(graph)
        assert row["content_hash"] == expected_hash and row["final_byte_coverage"] == "verified"
        assert row["byte_end"] > row["byte_start"] >= 0
        assert row["visibility_basis"] == "lexical_inference"
    assert payload[by_slot["u1"]["byte_start"]:by_slot["u1"]["byte_end"]].startswith("🌙 Oslo".encode())
    assert payload[by_slot["u2"]["byte_start"]:by_slot["u2"]["byte_end"]] == b"- how did tesla close (not dispatched)"
    assert by_slot["u1"]["disposition"] == "answered" and by_slot["u2"]["disposition"] == "unknown"
    assert "Oslo" not in json.dumps(verdict) and "tesla" not in json.dumps(verdict)


@pytest.mark.parametrize("state", ["indeterminate", "satisfied"])
def test_paraphrased_story_stays_unverified_not_falsely_certified_or_accused(state, fresh_store) -> None:
    from core.finalization import _semantic_conservation_verdict

    text = "Write a four-sentence original story about a lighthouse keeper who discovers a clock running backwards."
    opened, _graph = _mint(text)
    sid, ver = opened["set_id"], opened["version"]
    _sweep_to(sid, ver, {"u1": state})
    content = "Elias opened the brass door. The hands ticked in reverse. Sunset became dawn. The mechanism locked."
    verdict = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, content, {"session_id": "unverified"})
    assert verdict["slot_conservation"]["conserved"] is True
    assert verdict["publication_conservation"]["conserved"] is False
    (row,) = verdict["slot_evidence"]
    assert row["ledger_state"] == state
    assert row["disposition"] == "unverified"
    assert row["fulfillment_evidence"] == "indeterminate" and row["final_byte_coverage"] == "indeterminate"
    assert row["content_hash"] == "" and row["byte_start"] == row["byte_end"] == -1


@pytest.mark.parametrize("cancelled,enforced", [(False, False), (False, True), (True, False)])
def test_finalization_provenance_describes_committed_bytes_after_cancellation(cancelled, enforced, monkeypatch, fresh_store) -> None:
    import hashlib
    import threading

    from core.finalization import (
        CANCELLED_BEFORE_PUBLICATION,
        CONSERVATION_ENV,
        CONSERVATION_EVENT,
        finalize_answer,
        get_finalization_by_semantic_id,
    )
    from core.presentation_selection import select_presentation
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    text = "what is 18^2"
    opened, _graph = _mint(text)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    _sweep_to(sid, ver, {"u1": "satisfied"})
    marker = threading.Event()
    if cancelled:
        marker.set()
    session = "commit-byte-proof-" + str(cancelled)
    context = {"session_id": session, "runtime_session_id": session, "cancel_event": marker}
    content = "18² = 324"
    if enforced:
        monkeypatch.setenv(CONSERVATION_ENV, "enforce")
    reset_admission()
    try:
        record = admit_semantic_result({"response": content})
        semantic_id = record["_semantic_admission"]["semantic_result_id"]
        commit = finalize_answer(
            turn_id="turn-1", canonical_content=content, source_context=context,
            closure={"set_id": sid, "set_version": ver},
        )
        final = CANCELLED_BEFORE_PUBLICATION if cancelled else content
        stored = get_finalization_by_semantic_id(semantic_id)
        assert stored["canonical_content"] == final
        assert commit["content_hash"] == stored["content_hash"] == "sha256:" + hashlib.sha256(final.encode()).hexdigest()
        events = [event for event in _events(session) if event.get("event_type") == CONSERVATION_EVENT]
        assert len(events) == 1
        verdict = events[0]["verdict"]
        assert verdict["final_content_hash"] == commit["content_hash"]
        (proof,) = verdict["slot_evidence"]
        assert verdict["publication_conservation"]["conserved"] is not cancelled
        if cancelled:
            assert proof["disposition"] == "unverified" and proof["final_byte_coverage"] == "indeterminate"
            assert proof["content_hash"] == "" and proof["byte_start"] == -1
        else:
            assert proof["content_hash"] == commit["content_hash"]
            assert final.encode()[proof["byte_start"]:proof["byte_end"]] == content.encode()
        assert commit["display_metadata"]["presentation_selection"] == select_presentation(final, context)
    finally:
        reset_admission()


def test_enforcement_still_refuses_unverified_publication_with_no_finalization_row(monkeypatch, fresh_store) -> None:
    from core import finalization
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    text = "Write an original story about a lighthouse keeper."
    opened, _graph = _mint(text)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    _sweep_to(sid, ver, {"u1": "indeterminate"})
    monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    reset_admission()
    try:
        record = admit_semantic_result({"response": "Elias opened the brass door."})
        semantic_id = record["_semantic_admission"]["semantic_result_id"]
        with pytest.raises(finalization.FinalizationRejected, match="SLOT_CONSERVATION:publication"):
            finalization.finalize_answer(
                turn_id="turn-1", canonical_content="Elias opened the brass door.",
                closure={"set_id": sid, "set_version": ver},
            )
        assert finalization.get_finalization_by_semantic_id(semantic_id) is None
    finally:
        reset_admission()


@pytest.mark.parametrize("corruption", ["stale_hash", "foreign_turn", "duplicate"])
def test_invalid_span_proof_cannot_reach_durable_finalization(corruption, monkeypatch, fresh_store) -> None:
    from dataclasses import replace

    from core import finalization
    from core.semantic.bridges import publication
    from core.semantic.semantic_result_seam import admit_semantic_result, reset_admission

    text = "what is 18^2"
    opened, _graph = _mint(text)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    _sweep_to(sid, ver, {"u1": "satisfied"})
    monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    real_bridge = publication.published_slots

    def corrupt_proof(*args, **kwargs):
        records = real_bridge(*args, **kwargs)
        assert len(records) == 1 and records[0].visible
        if corruption == "duplicate":
            return (*records, records[0])
        change = {"content_hash": "sha256:stale"} if corruption == "stale_hash" else {"turn_id": "foreign"}
        return (replace(records[0], **change),)

    # Fault injection only: retain the actual bridge and finalization/DB/gates, corrupt its proof.
    monkeypatch.setattr(publication, "published_slots", corrupt_proof)
    reset_admission()
    try:
        record = admit_semantic_result({"response": "18² = 324"})
        semantic_id = record["_semantic_admission"]["semantic_result_id"]
        with pytest.raises(finalization.FinalizationRejected, match="SLOT_CONSERVATION:publication"):
            finalization.finalize_answer(
                turn_id="turn-1", canonical_content="18² = 324", closure={"set_id": sid, "set_version": ver},
            )
        assert finalization.get_finalization_by_semantic_id(semantic_id) is None
    finally:
        reset_admission()


def test_an_open_slot_is_missing_from_the_terminal_set(fresh_store) -> None:
    from core.finalization import _semantic_conservation_verdict

    opened, _graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    _sweep_to(sid, ver, {"u1": "satisfied"})  # u2 left planned
    verdict = _semantic_conservation_verdict({"set_id": sid, "set_version": ver}, "Oslo weather: 12 C.", {"session_id": "k"})
    assert verdict is not None and verdict["status"] == "evaluated"
    assert verdict["slot_conservation"]["conserved"] is False and verdict["slot_conservation"]["missing"] == ["u2"]


def test_enforce_flag_makes_an_unconserved_turn_refuse_at_finalization(monkeypatch, fresh_store) -> None:
    """The gates bite only when the operator says so; the default records and ships."""
    from core import finalization

    opened, _graph = _mint(TEXT)
    sid, ver = opened["set_id"], opened["version"]
    _sweep_to(sid, ver, {"u1": "satisfied", "u2": "unanswered"})
    closure = {"set_id": sid, "set_version": ver}
    monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    verdict = finalization._semantic_conservation_verdict(closure, "Oslo weather: 12 C.", {"session_id": "k"})
    assert verdict is not None and verdict["enforced"] is True and verdict["status"] == "evaluated"
    assert verdict["publication_conservation"]["conserved"] is False
    monkeypatch.delenv(finalization.CONSERVATION_ENV)
    relaxed = finalization._semantic_conservation_verdict(closure, "Oslo weather: 12 C.", {"session_id": "k"})
    assert relaxed is not None and relaxed["enforced"] is False


def test_a_set_without_a_graph_is_reported_not_evaluated_never_green(fresh_store) -> None:
    from core.finalization import CONSERVATION_EVENT, _semantic_conservation_verdict

    opened, _graph = _mint(TEXT, with_graph=False)
    closure = {"set_id": opened["set_id"], "set_version": opened["version"]}
    verdict = _semantic_conservation_verdict(closure, "x", {"session_id": "keystone-noeval", "runtime_session_id": "keystone-noeval"})
    assert verdict is not None and verdict["status"] == "not_evaluated" and verdict["reason"] == "no_graph"
    assert "slot_conservation" not in verdict
    assert [e for e in _events("keystone-noeval") if e.get("event_type") == CONSERVATION_EVENT], "a missing check is reported, not skipped"


def test_an_unreadable_stored_graph_is_reported_not_evaluated(fresh_store) -> None:
    from core.finalization import _semantic_conservation_verdict

    units = demand_units(TEXT)
    graph = lexical_request_graph(TEXT, turn_id="turn-1")
    rows = demand_obligation_rows(graph, attempt_id="att", request_text=TEXT, units={u.unit_id: u for u in units})
    payload = {**snapshot_graph_payload(graph), "serialization": "future.v999"}
    opened = ol.open_obligation_set(request_text=TEXT, request_id="req-u", obligations=rows, request_graph=payload)
    verdict = _semantic_conservation_verdict({"set_id": opened["set_id"], "set_version": opened["version"]}, "x", {"session_id": "k"})
    assert verdict is not None and verdict["status"] == "not_evaluated" and verdict["reason"] == "unreadable_graph"


# -- end to end: a driven turn mints the graph at the door ---------------------------------------


def test_a_driven_turn_mints_the_graph_at_the_door_and_reports_it(make_agent_module, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_SEMANTIC_REACH", "1")
    session = "keystone-e2e"
    agent = make_agent_module()
    result = agent.run_once(
        TEXT,
        session_id_override=session,
        source_context={"surface": "cli", "session_id": session, "runtime_session_id": session},
    )
    assert isinstance(result, dict)
    conn = sdb.get_connection()
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM obligation_sets ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["request_text"] == TEXT
    assert isinstance(snapshot.get("request_graph"), dict), "the door did not persist the graph"
    ids, source = frozen_slot_ids({"request_text": TEXT, "request_graph": snapshot["request_graph"]})
    assert source == SOURCE_GRAPH and ids == {u.unit_id for u in demand_units(TEXT)}
    assert {o["unit_id"] for o in snapshot["obligations"] if o.get("kind") == "demand"} == ids
    receipts = [json.loads(e["receipt"]) if isinstance(e.get("receipt"), str) else e.get("receipt")
                for e in _events(session) if e.get("event_type") == RESOLUTION_RECEIPT_EVENT]
    assert receipts, "the driven turn wrote no resolution receipt"
    receipt = receipts[-1]
    assert receipt["shape"] == "multi_clause"
    assert receipt["semantic"]["graph"]["producer"] == PRODUCER_NAME
    assert receipt["semantic"]["graph"]["failed"] == ""
    assert sorted(receipt["semantic"]["graph"]["slot_ids"]) == sorted(ids)
    # The context projection is what the receipt read; it carries no user text.
    context = result.get("source_context") if isinstance(result.get("source_context"), dict) else None
    if context is not None:
        projection = current_graph_projection(context)
        assert projection is not None and "tesla" not in json.dumps(projection).lower()
        assert REQUEST_GRAPH_KEY in context
