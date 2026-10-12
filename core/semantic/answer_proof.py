"""Bounded proof of a whole-turn executable answer contract over final bytes.

This is not a general truth oracle. Exact user literals and the existing closed-world parser/render
contract are executable; free-form prose, model review, shape checks and lexical coverage are not
semantic proof. A different but possibly correct paraphrase abstains rather than being called false.
Nothing here dispatches, repairs output, changes ledger states or authorizes publication.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from core.closed_world_semantic_contract import closed_world_semantic_response
from core.raw_output_contract import apply_raw_output_contract, parse_raw_output_contract
from core.semantic.graph_serialization import graph_digest
from core.semantic.request_graph import RequestGraph

# Whole-directive admission is deliberately narrower than the presentation parser. That parser can
# find a literal inside an explanation or one clause of a mixed request; neither proves the turn.
_LITERAL_DIRECTIVE = re.compile(
    r"\s*(?:output|return|say|print|reply\s+with|respond\s+with)\s+(?:exactly\s+)?"
    r"(?P<quote>['\"])?(?P<literal>[^'\"]{1,240}?)(?(quote)(?P=quote))"
    r"\s+and\s+nothing\s+else[.!]?\s*", re.IGNORECASE,
)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def answer_proof(graph: RequestGraph, *, request_text: str, final_content: str) -> dict[str, Any]:
    """Return text-free evidence bound to the stored graph and the complete final payload.

    ``verified`` means ONLY exact executable contract output, conditional on that contract's
    implementation and stated premises. It is neither independent world-fact verification nor
    per-slot proof of a model's interpretation. Finalization must obtain the graph/request from the
    frozen ledger, not caller metadata. No substring extraction, stripping or Unicode normalization
    of answer bytes is allowed: added contradictions, lost text and decorations cannot earn proof.
    """
    proof: dict[str, Any] = {
        "schema": "vool.semantic_answer_proof.v1",
        "scope": "whole_turn_contract_output",
        "status": "unverified",
        "reason": "no_executable_answer_contract",
        "evidence_grade": "none",
        "verifier": "none",
        "turn_id": str(graph.turn_id),
        "graph_digest": graph_digest(graph),
        "slot_ids": [str(slot.id) for slot in graph.slots],
        "request_hash": _hash(request_text),
        "final_content_hash": _hash(final_content),
    }
    if not graph.turn_id or not graph.slots or graph.canonical.normalize(request_text) != graph.canonical.text:
        proof["reason"] = "invalid_request_binding"
        return proof
    if graph.uncovered_source or graph.retractions:
        proof["reason"] = "unsupported_graph_structure"
        return proof
    try:
        contract = parse_raw_output_contract(request_text)
        if contract is not None and contract.exact_text is not None:
            directive = _LITERAL_DIRECTIVE.fullmatch(request_text)
            if directive is None or directive.group("literal") != contract.exact_text:
                proof["reason"] = "unsupported_literal_directive"
                return proof
            expected = contract.exact_text
            proof["verifier"] = "exact_user_literal.v1"
        else:
            expected = closed_world_semantic_response(request_text)
            if expected is None:
                return proof
            proof["verifier"] = "closed_world_contract_replay.v1"
        proof["expected_content_hash"] = _hash(expected)
        if final_content != expected:
            proof["reason"] = "not_exact_contract_output"
            return proof
        if contract is not None:
            application = apply_raw_output_contract(final_content, contract)
            if not application.compliant or application.rejected or application.text != final_content:
                proof["reason"] = "output_contract_not_satisfied"
                return proof
        proof.update(status="verified", reason="exact_contract_output", evidence_grade="deterministic_contract_replay")
    except Exception as exc:
        # A missing/broken checker must not manufacture proof or erase the other verdicts.
        proof.update(status="unverified", reason=f"verifier_error:{type(exc).__name__}", evidence_grade="none")
    return proof


__all__ = ["answer_proof"]
