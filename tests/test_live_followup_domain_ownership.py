"""Domain words are re-asks only within the live question they actually name."""
from __future__ import annotations

import uuid

import pytest

from core.live_data_continuation import continuation_inherits_live_data
from core.runtime_continuity import remember_live_data_obligation


def _context(operation: str, recorded: bool, *followups: str) -> tuple[str, dict]:
    request = "what is gold price now?" if operation == "market_quote" else "Get weather for Kaunas."
    entity = "Gold" if operation == "market_quote" else "Kaunas"
    session = f"domain-ownership:{uuid.uuid4().hex}"
    if recorded:
        remember_live_data_obligation(
            session, operation=operation, slots=[entity], request_text=request, absorbed_text=request,
        )
    messages = [{"role": "user", "content": request}, {"role": "assistant", "content": "Observed value."}]
    messages.extend({"role": "user", "content": text} for text in followups)
    return request, {"session_id": session, "runtime_session_id": session, "conversation_history": messages}


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,followup", (
    ("market_quote", "and the weather there?"),
    ("market_quote", "and the temperature?"),
    ("market_quote", "temp?"),
    ("weather_lookup", "and the price?"),
    ("weather_lookup", "value?"),
    ("weather_lookup", "what about the news?"),
    ("market_quote", "news?"),
))
def test_another_domain_never_reasks_the_old_question(operation, followup, recorded):
    _, context = _context(operation, recorded, followup)
    assert continuation_inherits_live_data(followup, source_context=context) == ""


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,followup", (
    ("weather_lookup", "temp?"),
    ("weather_lookup", "and the weather there?"),
    ("weather_lookup", "and the temperature?"),
    ("market_quote", "and the price?"),
    ("market_quote", "value?"),
    ("weather_lookup", "and?"),
    ("market_quote", "and?"),
))
def test_same_domain_and_neutral_reasks_keep_the_original_request(operation, followup, recorded):
    request, context = _context(operation, recorded, followup)
    assert continuation_inherits_live_data(followup, source_context=context) == request


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,followup", (
    ("market_quote", "i asked about golf weather, them two right?"),
    ("weather_lookup", "i asked about kauans price, them two right?"),
))
def test_a_long_cross_domain_turn_cannot_enter_the_clarification_path(operation, followup, recorded):
    _, context = _context(operation, recorded, followup)
    assert continuation_inherits_live_data(followup, source_context=context) == ""


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,interruption", (
    ("market_quote", "and the weather there?"),
    ("weather_lookup", "and the price?"),
    ("weather_lookup", "what about the news?"),
))
def test_a_later_nudge_cannot_walk_back_through_a_cross_domain_question(operation, interruption, recorded):
    _, context = _context(operation, recorded, interruption, "and?")
    assert continuation_inherits_live_data("and?", source_context=context) == ""


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,followup", (
    ("weather_lookup", "temp?"),
    ("market_quote", "and the price?"),
))
def test_a_later_nudge_can_still_walk_back_through_a_same_domain_reask(operation, followup, recorded):
    request, context = _context(operation, recorded, followup, "and?")
    assert continuation_inherits_live_data("and?", source_context=context) == request


@pytest.mark.parametrize("recorded", (False, True))
@pytest.mark.parametrize("operation,followup", (
    ("market_quote", "and the weather there?"),
    ("weather_lookup", "and the price?"),
    ("weather_lookup", "what about the news?"),
))
def test_a_declined_domain_keeps_canonical_units_without_claims_or_execution(operation, followup, recorded):
    from core.agent_runtime.answer_coverage import demand_units
    from core.agent_runtime.live_data_render import render_live_data_answer
    from core.agent_runtime.live_data_runner import run_live_data_plan
    from core.execution_requirements import requirements_for
    from core.live_data_plan import build_live_data_plan, evaluate_approval_policy, lane_proposal_for_plan

    _, context = _context(operation, recorded, followup)
    units = demand_units(followup)
    assert units
    assert build_live_data_plan(followup, plan_id="p", attempt_id="a", source_context=context) is None
    plan = build_live_data_plan(
        followup, plan_id="p", attempt_id="a", source_context=context, canonical_units=units,
    )
    assert plan is not None
    assert plan.original_request == followup
    assert plan.subtasks == ()
    assert plan.unclaimed_unit_ids == tuple(unit.unit_id for unit in units)
    proposal = lane_proposal_for_plan(plan, requirements_for(followup, source_context=context))
    assert proposal.obligations_claimed == ()
    assert proposal.effects_requested == ()
    assert proposal.unclaimed_obligations == plan.unclaimed_unit_ids
    assert evaluate_approval_policy(plan, source_context=context) == {}
    assert run_live_data_plan(plan) == []
    assert render_live_data_answer(plan, []) == ""


@pytest.mark.parametrize("multipart", (False, True))
def test_execution_entrypoints_decline_an_empty_conservation_plan(monkeypatch, multipart):
    from unittest.mock import Mock

    from core.agent_runtime import live_data_runner
    from core.agent_runtime.agent import VoolAgent
    from core.execution_requirements import requirements_for

    followup = "and the weather there?"
    _, context = _context("market_quote", True, followup)
    agent = Mock(spec=VoolAgent)
    agent._create_live_data_runtime_attempt.return_value = "attempt"
    agent._client_turn_id.return_value = "turn"
    agent._kernel_allows_lane.return_value = (True, "")
    agent._live_data_plan_unavailable_result.return_value = {"response": "unavailable"}
    agent._prepend_unserved_slice_answers.side_effect = VoolAgent._prepend_unserved_slice_answers
    run = Mock(wraps=live_data_runner.run_live_data_plan)
    monkeypatch.setattr(live_data_runner, "run_live_data_plan", run)
    arguments = {"effective_input": followup, "raw_input": followup,
                 "session_id": context["session_id"], "source_context": context}
    if multipart:
        result = VoolAgent._answer_multipart_live_data_turn(
            agent, **arguments, requirements=requirements_for(followup, source_context=context),
        )
        assert result == {"response": "unavailable"}
    else:
        result = VoolAgent._answer_single_live_data_turn(agent, **arguments)
        assert result is None
    agent._record_live_data_plan_subtasks.assert_not_called()
    run.assert_not_called()
    agent._finalize_live_data_runtime_attempt.assert_called_once_with(
        "attempt", plan_valid=False, client_turn_id="turn",
    )
