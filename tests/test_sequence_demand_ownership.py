"""Sequence coverage must serve dependent steps without absorbing independent asks."""
from __future__ import annotations

import pytest

from core.agent_runtime.demand_ownership import (
    LANE_DIRECT_MATH,
    demand_coverage,
    lane_may_claim_whole_turn,
)
from core.task_router import evaluate_sequence_ops_request
from tests.test_sequence_ops_deterministic_lane import MEASURED_TURN, REAL_OPERATOR_TURN


@pytest.mark.parametrize("turn", (MEASURED_TURN, REAL_OPERATOR_TURN))
def test_complete_sequence_owns_its_declaration_and_steps(turn):
    assert evaluate_sequence_ops_request(turn, require_complete=True) == "[4,3,10]"
    assert lane_may_claim_whole_turn(turn, LANE_DIRECT_MATH)


@pytest.mark.parametrize("turn", (
    "Explain entropy. My list is [1,2]. Reverse it. Output the array, no spaces.",
    "My list. Explain entropy. Array A is [1,2]. Reverse it. Output the array.",
    "My list is [1,2]. Reverse it. Explain entropy. Output the array, no spaces.",
    "My list is [1,2]. Reverse it. Output the array, no spaces. Explain entropy.",
    "My list is [1,2]. Reverse it. Output the array. Tell me a haiku now.",
    "My list is [1,2]. Reverse it. Output the array. Delete notes.txt.",
    "My list is [1,2]. Reverse it. Output the array. What is the weather now?",
))
def test_unserved_text_never_proves_whole_sequence_coverage(turn):
    assert evaluate_sequence_ops_request(turn, require_complete=True) is None
    # Single-unit turns keep the catalog's existing terminal eligibility. The
    # strict reader must contribute no new coverage, regardless of that gate.
    assert not demand_coverage(turn).lane_unit_ids(LANE_DIRECT_MATH)


def test_unknown_operation_cannot_supply_sequence_coverage():
    assert evaluate_sequence_ops_request(
        "My list is [1,2]. Sort by relevance. Output the array.", require_complete=True
    ) is None


def test_unavailable_sequence_reader_does_not_bind_demands(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError("synthetic reader failure")

    monkeypatch.setattr("core.task_router.evaluate_sequence_ops_request", unavailable)
    assert not lane_may_claim_whole_turn(REAL_OPERATOR_TURN, LANE_DIRECT_MATH)
