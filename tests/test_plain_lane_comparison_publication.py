"""Comparison publication shares one render without erasing facet obligations."""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.agent_runtime.answer_coverage import demand_units
from core.agent_runtime.demand_ownership import execution_unit_spans
from tests.test_plain_lane_stable_units import ANSWER_14, TURN_14, _body, _publish


@pytest.mark.parametrize("eligible", [True, False])
def test_one_stable_comparison_render_retains_every_original_obligation(eligible):
    before = demand_units(TURN_14)
    group, = execution_unit_spans(TURN_14)
    verdict, stable, computed = _publish(TURN_14, ANSWER_14, eligible=eligible)
    assert len(before) == 3 and len(stable) == 1 and computed == []
    assert stable[0]["accounted_unit_ids"] == list(group.member_unit_ids)
    assert stable[0]["accounted_unit_ids"] == [unit.unit_id for unit in before]
    assert stable[0]["demand_id"] == group.unit_id and stable[0]["demand_text"] == TURN_14
    assert stable[0]["author_eligible"] is eligible
    assert demand_units(TURN_14) == before
    body = _body(verdict)
    if eligible:
        assert "cost more to buy" in body and "travel farther" in body and "tailpipe" in body
    else:
        assert "cost more to buy" not in body and "tailpipe" not in body
    assert "37 million" not in body and "1974" not in body


@pytest.mark.parametrize(("claim", "status"), [
    ("Electric car sales today are rising worldwide.", "uncertain"),
    ("Electric cars have a typical range of 253 miles.", "unsupported"),
    ("Electric cars cost 37 million today.", "unsupported"),
])
def test_a_grouped_comparison_cannot_exempt_current_or_measured_claims(claim, status):
    verdict, stable, _computed = _publish(TURN_14, ANSWER_14 + "\n" + claim, eligible=True)
    assert len(stable) == 1
    assert "cost more to buy" in _body(verdict) and "tailpipe emissions" in _body(verdict)
    assert claim not in _body(verdict)
    assert any(row["text"] == claim and row["status"] == status for row in verdict.claim_support["claims"])
    assert claim not in verdict.exempted_claims
    assert claim in verdict.content.split("Withheld from this answer:", 1)[1]
    if status == "unsupported":
        assert any(claim in withheld for withheld in verdict.withheld_claims)


@pytest.mark.parametrize("members", [("u1", "u2"), ("u1", "u2", "foreign")])
def test_missing_or_foreign_group_members_do_not_gain_a_shared_render(monkeypatch, members):
    group, = execution_unit_spans(TURN_14)
    monkeypatch.setattr("core.agent_runtime.demand_ownership.execution_unit_spans",
                        lambda _text: (replace(group, member_unit_ids=members),))
    _verdict, stable, _computed = _publish(TURN_14, ANSWER_14, eligible=True)
    assert len(stable) == 3
    assert not any(len(entry.get("accounted_unit_ids", [])) > 1 for entry in stable)


def test_arithmetic_keeps_its_computed_channel_beside_stable_knowledge():
    verdict, stable, computed = _publish(
        "What is 5+5? In what year did the Berlin Wall fall?",
        "5+5 = 10.\nThe Berlin Wall fell in 1989.", eligible=True,
    )
    assert len(stable) == len(computed) == 1
    assert "Berlin Wall" in stable[0]["demand_text"] and "10" in computed[0]["summary"]
    assert "5+5 = 10" in _body(verdict) and "1989" in _body(verdict)


def test_a_current_member_cannot_be_classified_by_its_stable_siblings():
    request = TURN_14.rstrip(".") + " right now."
    verdict, stable, _computed = _publish(request, ANSWER_14, eligible=True)
    assert not any(len(entry.get("accounted_unit_ids", [])) > 1 for entry in stable)
    assert "37 million" not in _body(verdict)
