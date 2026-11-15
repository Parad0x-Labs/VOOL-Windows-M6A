"""On Windows, a chat request for Apple Calendar, the Skill Codex or VOOL Drop gets one fixed answer and no model.

Measured on Steve, 14 Nov 2026 (0.7.2 at 0360e41e): each of the three reached the model, which answered in prose
as if the feature worked. Off Windows the lane stands down, so macOS and Linux are unchanged.
"""
from __future__ import annotations

import pytest

from core import posix_tools
from core.agent_runtime import fast_paths_windows_unavailable as lane

_REQUESTS = (
    ("connect my Apple Calendar account", lane.WINDOWS_APPLE_CALENDAR_ANSWER, "apple_calendar_unavailable_on_windows"),
    ("can you sync my apple calendar", lane.WINDOWS_APPLE_CALENDAR_ANSWER, "apple_calendar_unavailable_on_windows"),
    ("open the Skill Codex and install a skill", lane.WINDOWS_SKILL_CODEX_ANSWER, "skill_codex_unavailable_on_windows"),
    ("install a skill from the skill codex", lane.WINDOWS_SKILL_CODEX_ANSWER, "skill_codex_unavailable_on_windows"),
    ("send this file to my phone with VOOL Drop", lane.WINDOWS_DROP_ANSWER, "drop_unavailable_on_windows"),
    ("turn on vool drop", lane.WINDOWS_DROP_ANSWER, "drop_unavailable_on_windows"),
)
_NOT_REQUESTS = (
    "what is the Skill Codex?",
    "connect my iCloud calendar",
    "add my Google calendar",
    "drop the last paragraph",
    "tell me about VOOL Drop",
)


class _FastPathAgent:
    def _fast_path_result(self, **kwargs):
        return dict(kwargs)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)


@pytest.mark.parametrize(("text", "answer", "reason"), _REQUESTS)
def test_on_windows_the_request_gets_its_fixed_answer(windows, text, answer, reason) -> None:
    result = lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={})
    assert result is not None, text
    assert result["response"] == answer
    assert result["reason"] == reason


@pytest.mark.parametrize("text", _NOT_REQUESTS)
def test_on_windows_a_message_this_lane_does_not_own_is_left_alone(windows, text) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={}) is None


@pytest.mark.parametrize(("text", "answer", "reason"), _REQUESTS)
def test_off_windows_the_lane_stands_down(off_windows, text, answer, reason) -> None:
    assert lane.maybe_refuse_windows_unavailable(_FastPathAgent(), text, session_id="s", source_context={}) is None


def test_on_windows_the_front_door_answers_without_a_model(windows) -> None:
    from types import SimpleNamespace as Namespace
    from unittest import mock

    from apps.vool_agent import VoolAgent

    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    for text, answer, _reason in _REQUESTS:
        with mock.patch.object(agent, "_startup_sequence_fast_path", return_value=None), mock.patch(
            "core.agent_runtime.agent.maybe_handle_preference_command", return_value=(False, "")
        ), mock.patch.object(agent, "_maybe_handle_credit_command", return_value=None), mock.patch.object(
            agent, "_maybe_handle_workspace_audit_request", return_value=None
        ):
            outcome = agent._handle_turn_frontdoor(
                raw_user_input=text,
                effective_input=text,
                normalized_input=text,
                source_surface="openclaw",
                session_id="windows-unavailable",
                source_context={"surface": "openclaw", "_owner_local": True},
                persona=mock.sentinel.persona,
                interpreted=Namespace(understanding_confidence=0.8),
            )
        result = outcome.get("result")
        assert result is not None, f"the front door let {text!r} fall through to a model"
        assert str(result.get("response") or "") == answer, text
