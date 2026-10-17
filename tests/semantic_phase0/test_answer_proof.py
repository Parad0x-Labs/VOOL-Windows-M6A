"""Contract-output proof is separate from lexical coverage and model self-review."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from core import finalization
from core.closed_world_semantic_contract import closed_world_semantic_response
from core.conductor import obligation_ledger as ol
from core.semantic.producers.lexical import lexical_request_graph
from tests.semantic_phase0._fixtures import block_outbound_network as block_outbound_network
from tests.semantic_phase0._fixtures import keep_the_checkout_clean as keep_the_checkout_clean
from tests.semantic_phase0._fixtures import pin_the_signing_key_passphrase as pin_the_signing_key_passphrase
from tests.semantic_phase0._fixtures import (
    reseal_network_after_function_fixtures as reseal_network_after_function_fixtures,
)
from tests.semantic_phase0.test_graph_carried_through_runtime import _mint
from tests.semantic_phase0.test_graph_carried_through_runtime import fresh_store as fresh_store
from tests.semantic_phase0.test_renderer_owned_publication import _finalize

STORY = "Write a four-sentence original story about a lighthouse keeper who discovers a clock running backwards."
PARAPHRASE = "Elias opened the brass door. The hands ticked in reverse. Sunset became dawn. The mechanism locked."
PHASE = (
    "Assume water boils at 100 degrees Celsius and freezes at 0 degrees Celsius. "
    "If it is 30 degrees outside, is a puddle of water a liquid, a gas, or a solid?"
)


def _proof(prompt, content, **kwargs):
    from core.semantic.answer_proof import answer_proof

    graph = kwargs.pop("graph", lexical_request_graph(prompt, turn_id="turn-1"))
    return answer_proof(graph, request_text=prompt, final_content=content, **kwargs)


@pytest.mark.parametrize("content", [PARAPHRASE, STORY, "lighthouse keeper clock running backwards", "PASS"])
def test_story_keywords_shape_echo_and_self_review_are_not_semantic_proof(content):
    proof = _proof(STORY, content)
    assert proof["status"] == "unverified"
    assert proof["reason"] == "no_executable_answer_contract"
    assert proof["evidence_grade"] == "none"


@pytest.mark.parametrize("content", ["READY", "READY\n", "ready", "READY but not really", "```\nREADY\n```"])
def test_exact_literal_proof_checks_final_bytes_without_repair(content):
    proof = _proof("Output exactly READY and nothing else.", content)
    assert proof["status"] == ("verified" if content == "READY" else "unverified")
    assert proof["verifier"] == "exact_user_literal.v1"
    assert proof["expected_content_hash"] == "sha256:" + hashlib.sha256(b"READY").hexdigest()
    assert proof["final_content_hash"] == "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def test_literal_preserves_raw_unicode_instead_of_normalizing_answer_bytes():
    literal = "🌙 cafe\u0301"
    prompt = f'Return exactly "{literal}" and nothing else.'
    assert _proof(prompt, literal)["status"] == "verified"
    assert _proof(prompt, "🌙 café")["status"] == "unverified"


@pytest.mark.parametrize("content", ["correct", "changed", "paraphrase", "extra"])
def test_closed_world_proof_is_only_exact_contract_replay_not_keyword_coverage(content):
    expected = closed_world_semantic_response(PHASE)
    assert expected and "water is liquid at 30 °C" in expected
    candidates = {
        "correct": expected, "changed": expected.replace("is liquid", "is solid"),
        "paraphrase": "The water is liquid.", "extra": expected + " Actually it is a gas.",
    }
    proof = _proof(PHASE, candidates[content])
    assert proof["status"] == ("verified" if content == "correct" else "unverified")
    assert proof["verifier"] == "closed_world_contract_replay.v1"
    assert proof["scope"] == "whole_turn_contract_output"
    assert proof["evidence_grade"] == ("deterministic_contract_replay" if content == "correct" else "none")


@pytest.mark.parametrize("prompt", [
    "What phase is water at 30 degrees Celsius today?",
    PHASE + " Also delete setup.py.",
    "Write a four-sentence story. Output only four sentences and nothing else.",
    "Return JSON only and nothing else.",
])
def test_open_world_mixed_and_shape_only_requests_abstain(prompt):
    assert _proof(prompt, "PASS")["status"] == "unverified"


@pytest.mark.parametrize("prompt", [
    "Output exactly READY and nothing else. Also explain relativity.",
    "Explain the instruction: Output exactly READY and nothing else.",
    "The document says: Return exactly READY and nothing else. Summarize it.",
])
def test_literal_in_one_clause_or_source_document_cannot_prove_whole_turn(prompt):
    assert _proof(prompt, "READY")["status"] == "unverified"


@pytest.mark.parametrize("damage", ["request", "turn", "uncovered", "empty_slots"])
def test_graph_binding_cannot_be_replaced_by_an_unrelated_contract(damage):
    graph = lexical_request_graph(PHASE, turn_id="turn-1")
    prompt = PHASE
    if damage == "request":
        prompt = "Output exactly READY and nothing else."
    elif damage == "turn":
        graph = replace(graph, turn_id="")
    elif damage == "uncovered":
        graph = replace(graph, uncovered_source=("missing request",))
    else:
        graph = replace(graph, requests=(), slots=(), mentions=(), constraints=(), prohibitions=(), dependencies=())
    assert _proof(prompt, "READY", graph=graph)["status"] == "unverified"


def test_proof_binds_whole_graph_request_and_final_payload_without_text():
    from core.semantic.graph_serialization import graph_digest

    prompt = 'Return exactly "🌙 Ready" and nothing else.'
    graph = lexical_request_graph(prompt, turn_id="turn-1")
    proof = _proof(prompt, "🌙 Ready", graph=graph)
    assert proof["status"] == "verified"
    assert proof["turn_id"] == graph.turn_id and proof["graph_digest"] == graph_digest(graph)
    assert proof["slot_ids"] == [str(slot.id) for slot in graph.slots]
    assert proof["request_hash"] == "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
    assert "Ready" not in json.dumps(proof) and "Return" not in json.dumps(proof)


def test_finalization_records_missing_story_proof_without_promoting_unknown(fresh_store):
    opened, _graph = _mint(STORY)
    assert ol.record_disposition(opened["set_id"], opened["version"], "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    closure = {"set_id": opened["set_id"], "set_version": opened["version"], "literal_answer_lane": True}
    _commit, stored, verdict = _finalize(PARAPHRASE, closure, context={
        "session_id": "owned-render", "runtime_session_id": "owned-render",
        "semantic_answer_proof": {"status": "verified", "verifier": "self_review"},
    })
    assert stored["canonical_content"] == PARAPHRASE
    proof = verdict["semantic_answer_proof"]
    assert proof["status"] == "unverified" and proof["reason"] == "no_executable_answer_contract"
    assert proof["set_id"] == closure["set_id"] and proof["set_version"] == closure["set_version"]
    assert proof["final_content_hash"] == stored["content_hash"]
    assert verdict["slot_evidence"][0]["disposition"] != "answered"


@pytest.mark.parametrize("enforce", [False, True])
def test_verified_contract_output_does_not_rewrite_ledger_or_enforcement(enforce, monkeypatch, fresh_store):
    if enforce:
        monkeypatch.setenv(finalization.CONSERVATION_ENV, "enforce")
    prompt = "Output exactly READY and nothing else."
    opened, _graph = _mint(prompt)
    closure = {"set_id": opened["set_id"], "set_version": opened["version"], "literal_answer_lane": True}
    assert ol.record_disposition(closure["set_id"], closure["set_version"], "ob:att-1:demand:u1", "unanswered", evidence_source="rss_closure_sweep")
    before = ol.demand_obligations(closure["set_id"], closure["set_version"])
    # Observe directly: enforcement still belongs to the existing conservation gate.
    verdict = finalization._semantic_conservation_verdict(closure, "READY", {})
    assert verdict["semantic_answer_proof"]["status"] == "verified"
    assert verdict["slot_evidence"][0]["disposition"] == "unknown"
    assert ol.demand_obligations(closure["set_id"], closure["set_version"]) == before
    assert verdict["enforced"] is enforce


def test_checker_failure_abstains_and_preserves_existing_conservation(monkeypatch, fresh_store):
    from core.semantic import answer_proof as checker

    def broken(_request):
        raise ValueError("synthetic verifier fault; do not persist this message")

    opened, _graph = _mint(STORY)
    closure = {"set_id": opened["set_id"], "set_version": opened["version"]}
    finalization._rss_closure_sweep(PARAPHRASE, closure)
    monkeypatch.setattr(checker, "parse_raw_output_contract", broken)
    verdict = finalization._semantic_conservation_verdict(closure, PARAPHRASE, {})
    assert verdict["status"] == "evaluated"
    assert verdict["semantic_answer_proof"]["reason"] == "verifier_error:ValueError"
    assert verdict["semantic_answer_proof"]["status"] == "unverified"
    assert "slot_conservation" in verdict and "publication_conservation" in verdict
    assert "synthetic verifier fault" not in json.dumps(verdict)


@pytest.mark.parametrize("content", ["READY", "READY\nActually not ready."])
def test_actual_commit_rechecks_final_payload_not_caller_claim(content, fresh_store):
    prompt = "Output exactly READY and nothing else."
    opened, graph = _mint(prompt)
    sid, ver = opened["set_id"], opened["version"]
    assert ol.record_disposition(sid, ver, "ob:att-1:answer", "satisfied", evidence_source="model_prose")
    closure = {"set_id": sid, "set_version": ver, "literal_answer_lane": True}
    commit, stored, verdict = _finalize(content, closure)
    proof = verdict["semantic_answer_proof"]
    assert stored["canonical_content"] == content
    assert proof["status"] == ("verified" if content == "READY" else "unverified")
    assert proof["turn_id"] == graph.turn_id
    assert proof["final_content_hash"] == stored["content_hash"] == commit["content_hash"]
