"""Calendar readers declare the policy's canonical network action, never a local read."""
from __future__ import annotations

import pytest

from core.mode_permission_policy import (
    OperatingMode,
    PermissionAction,
    PermissionEffect,
    actions_for_tool,
    decide_tool_call,
    reset_mode_permission_state,
    set_active_mode,
)
from core.runtime_tool_contracts import runtime_tool_contracts

CALENDAR_READS = (
    "operator.check_availability",
    "operator.list_calendars",
    "operator.inspect_calendar_event",
    "operator.show_agenda",
    "operator.search_calendar_event",
)


@pytest.fixture(autouse=True)
def _clean_mode_state():
    reset_mode_permission_state()
    yield
    reset_mode_permission_state()


@pytest.mark.parametrize("intent", CALENDAR_READS)
def test_calendar_read_declares_the_canonical_network_action(intent):
    contract = {item.intent: item for item in runtime_tool_contracts()}[intent]
    assert contract.side_effect_class == "read_only"
    assert contract.permission_actions == (PermissionAction.USE_NETWORK.value,)
    assert set(actions_for_tool(intent, {})) == {PermissionAction.USE_NETWORK}


@pytest.mark.parametrize("intent", CALENDAR_READS)
@pytest.mark.parametrize(
    ("mode", "effect"),
    (
        (OperatingMode.PLAN, PermissionEffect.DENY),
        (OperatingMode.MANUAL, PermissionEffect.REQUIRE_APPROVAL),
        (OperatingMode.REVIEW_EDITS, PermissionEffect.REQUIRE_APPROVAL),
        (OperatingMode.AUTO, PermissionEffect.ALLOW),
    ),
)
def test_calendar_read_keeps_the_real_network_mode_gate(intent, mode, effect):
    session = "calendar-read-contract"
    set_active_mode(session, mode.value, client_turn_id="calendar-read-turn")
    decision = decide_tool_call(
        intent=intent,
        arguments={"query": "meeting", "window": "this week"},
        task_id="calendar-read-turn",
        source_context={"runtime_session_id": session, "operating_mode": mode.value},
    )
    assert set(decision.actions) == {PermissionAction.USE_NETWORK}
    assert decision.effect is effect
    if effect is PermissionEffect.REQUIRE_APPROVAL:
        assert decision.approval_request is not None
    else:
        assert decision.approval_request is None
