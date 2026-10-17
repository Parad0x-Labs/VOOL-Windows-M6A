"""Local draft review does not authorize the separate email-send effect."""
from __future__ import annotations

import pytest

from core import policy_engine
from core.mode_permission_policy import (
    OperatingMode,
    PermissionAction,
    PermissionEffect,
    actions_for_tool,
    preview_tool_call,
    reset_mode_permission_state,
    set_active_mode,
)
from core.runtime_tool_contracts import runtime_tool_contracts
from tests import test_read_only_tools_are_not_denied_by_mode as census

LOCAL_REVIEW_STATE = ("email.draft.save", "email.draft.get", "email.draft.approve")
MODES = (OperatingMode.MANUAL, OperatingMode.REVIEW_EDITS, OperatingMode.PLAN, OperatingMode.AUTO)


@pytest.fixture(autouse=True)
def _clean_mode_state():
    reset_mode_permission_state()
    yield
    reset_mode_permission_state()


@pytest.mark.parametrize("intent", LOCAL_REVIEW_STATE)
def test_local_review_state_stays_in_every_read_only_census(intent):
    contract = {item.intent: item for item in runtime_tool_contracts()}[intent]
    assert contract.side_effect_class == "read_only"
    assert contract.approval_requirement == "none"
    assert contract.permission_actions == ()
    assert actions_for_tool(intent, {}) == ()
    assert intent in census._read_only_intents()
    census.test_a_read_only_tool_never_classifies_as_an_unknown_side_effect(intent)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("intent", LOCAL_REVIEW_STATE)
def test_local_review_state_needs_no_mode_approval_and_authorizes_no_send(intent, mode):
    session = "draft-review-boundary"
    set_active_mode(session, mode.value, client_turn_id="review-turn")
    decision = preview_tool_call(
        intent=intent,
        arguments={"draft_id": "review-only", "expected_version": 1},
        task_id="review-turn",
        source_context={"runtime_session_id": session, "operating_mode": mode.value},
    )
    assert decision.mode is mode
    assert decision.effect is PermissionEffect.ALLOW
    assert decision.actions == ()
    assert not decision.would_consume_token
    assert not decision.would_request_approval
    send = preview_tool_call(
        intent="email.draft.send",
        arguments={"draft_id": "review-only"},
        task_id="review-turn",
        source_context={"runtime_session_id": session, "operating_mode": mode.value},
    )
    assert set(send.actions) == {PermissionAction.USE_NETWORK, PermissionAction.SEND_EXTERNAL_MESSAGES}
    assert send.effect is (PermissionEffect.DENY if mode is OperatingMode.PLAN else PermissionEffect.REQUIRE_APPROVAL)
    assert not send.would_consume_token
    assert send.would_request_approval is (mode is not OperatingMode.PLAN)


def test_send_and_reconciliation_keep_their_distinct_contracts():
    contracts = {item.intent: item for item in runtime_tool_contracts()}
    send = contracts["email.draft.send"]
    assert send.side_effect_class == "network_send"
    assert send.approval_requirement == "explicit_user_opt_in"
    assert send.retry_policy == "none"
    assert send.artifact_emission == "send_receipt"
    assert set(send.permission_actions) == {PermissionAction.USE_NETWORK.value, PermissionAction.SEND_EXTERNAL_MESSAGES.value}
    assert "email.draft.send" not in census._read_only_intents()
    assert "email.draft.reconcile" in census._read_only_intents()
    assert set(actions_for_tool("email.draft.reconcile", {})) == {
        PermissionAction.USE_NETWORK, PermissionAction.ACCESS_EXTERNAL_PROVIDERS,
    }


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("intent", (*LOCAL_REVIEW_STATE, "email.draft.send"))
def test_action_free_review_does_not_bypass_the_email_send_enablement(intent, enabled, monkeypatch):
    original = policy_engine.get
    monkeypatch.setattr(policy_engine, "get", lambda key, default=None: enabled if key == "email.send_enabled" else original(key, default))
    contract = {item.intent: item for item in runtime_tool_contracts()}[intent]
    assert contract.supported is enabled
    if not enabled:
        assert contract.unsupported_reason
    assert actions_for_tool(intent, {}) == (() if intent in LOCAL_REVIEW_STATE else (
        PermissionAction.USE_NETWORK, PermissionAction.SEND_EXTERNAL_MESSAGES,
    ))


@pytest.mark.parametrize("intent", ("email.draft.preview", "email.draft.approve_foreign", "unregistered.review_state"))
def test_unregistered_review_names_do_not_inherit_the_empty_action_exception(intent):
    assert actions_for_tool(intent, {}) == (PermissionAction.UNKNOWN_SIDE_EFFECT,)


@pytest.mark.parametrize("actions", ((), (PermissionAction.UNKNOWN_SIDE_EFFECT,)))
def test_the_census_still_rejects_an_unclassified_future_read(actions, monkeypatch):
    monkeypatch.setattr(census, "actions_for_tool", lambda name, arguments: actions)
    with pytest.raises(AssertionError):
        census.test_a_read_only_tool_never_classifies_as_an_unknown_side_effect("future.local_read")


@pytest.mark.parametrize("intent", LOCAL_REVIEW_STATE)
@pytest.mark.parametrize("action", (PermissionAction.READ_FILES, PermissionAction.CREATE_FILES, PermissionAction.USE_NETWORK))
def test_the_census_rejects_any_effect_added_to_action_free_review(intent, action, monkeypatch):
    monkeypatch.setattr(census, "actions_for_tool", lambda name, arguments: (action,))
    with pytest.raises(AssertionError, match="classified local review state as a permission effect"):
        census.test_a_read_only_tool_never_classifies_as_an_unknown_side_effect(intent)
