"""Windows: "are you online?" gets a fixed status answer from the front door, with no model call.

Consistency recheck, row 7 (Windows 11): "are you online?" got small talk ("I'm here, Jesse…") 3 of 3
times, and never said whether replies were coming from a local or a cloud model, or whether web lookup
was on. Now the front door answers it from the runtime's own state: the Windows cloud rule (a stored key
and cloud turned on), Local Only, whether a local model is running, and the web-search permission
(`allow_web_fallback`), which Local Only does not turn off. Other subjects ("is google online") are not
this question. Off Windows nothing changes.

Served end to end as the VOOL window sends it (/api/chat), with the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py; the model is stubbed and counted.
"""
from __future__ import annotations

import uuid

import pytest

from core import local_model_presence, policy_engine, windows_cloud_gate
from core import windows_write_gate as gate
from core.agent_runtime import fast_command_surface as fcs
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

ASKS = (
    "are you online",
    "are you online?",
    "Are you online right now?",
    "are you offline",
    "are you offline?",
    "are you connected to the internet?",
    "is web lookup on",
    "is web lookup on?",
    "is web search enabled?",
)
NOT_THIS = (
    "is the server online",
    "is google online?",
    "is my friend online",
    "are you connected?",
    "are you online shopping today",
    "tell me if you are online and then write a poem",
)


def _state(monkeypatch, *, local: bool, cloud: bool, web: bool, local_only: bool = False) -> None:
    monkeypatch.setattr(local_model_presence, "local_model_running", lambda **_k: local)
    monkeypatch.setattr(windows_cloud_gate, "cloud_key_stored", lambda: cloud)
    monkeypatch.setattr(windows_cloud_gate, "owner_turned_cloud_on", lambda: cloud)
    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: web)
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: local_only)


def _say(c, text: str) -> str:
    return c.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


@pytest.mark.parametrize("ask", ASKS)
def test_the_status_question_is_answered_from_state_with_no_model_call(machine, chat, monkeypatch, ask) -> None:
    _state(monkeypatch, local=True, cloud=False, web=True)
    c = chat(machine["desktop"])
    reply = _say(c, ask)
    assert c.model_rounds == 0, reply
    assert "Replies: from a local model on this computer" in reply, reply
    assert "cloud models are off" in reply, reply
    assert "Web lookup: on." in reply, reply
    assert "Jesse" not in reply and "I'm here" not in reply, reply


def test_the_answer_follows_the_state(machine, chat, monkeypatch) -> None:
    c = chat(machine["desktop"])

    _state(monkeypatch, local=True, cloud=False, web=False)
    reply = _say(c, "are you online?")
    assert "Replies: from a local model on this computer" in reply and "Web lookup: off." in reply, reply

    _state(monkeypatch, local=False, cloud=True, web=True)
    reply = _say(c, "are you online?")
    assert "Replies: from a cloud model" in reply and "Web lookup: on." in reply, reply

    _state(monkeypatch, local=True, cloud=True, web=True)
    reply = _say(c, "are you online?")
    assert "a reply can come from a cloud model" in reply, reply

    # Local Only keeps replies local even with cloud turned on, and does not turn web lookup off.
    _state(monkeypatch, local=True, cloud=True, web=True, local_only=True)
    reply = _say(c, "are you online?")
    assert "Replies: from a local model on this computer" in reply and "Local Only is on" in reply, reply
    assert "Web lookup: on." in reply and "web search still goes through the browser" in reply, reply

    _state(monkeypatch, local=False, cloud=False, web=False)
    reply = _say(c, "are you offline")
    assert "no local model is running on this computer and cloud models are off" in reply, reply
    assert "Web lookup: off." in reply, reply
    assert c.model_rounds == 0


def test_the_models_are_named_when_known(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _state(monkeypatch, local=True, cloud=True, web=True)
    from core import cloud_escalation_policy, usage_meter

    monkeypatch.setattr(usage_meter, "last_served_call", lambda: {"lane": "local", "model_id": "qwen2.5:7b"})
    policy = cloud_escalation_policy.load_policy()
    monkeypatch.setattr(cloud_escalation_policy, "load_policy", lambda: type(policy)(**{**policy.__dict__, "model": "x-ai/some-model"}))
    reply = fcs.maybe_handle_online_status_question("are you online?")
    assert reply is not None
    assert "(qwen2.5:7b)" in reply and "(x-ai/some-model)" in reply, reply


@pytest.mark.parametrize("ask", NOT_THIS)
def test_other_subjects_are_not_this_question(monkeypatch, ask) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert not fcs.is_online_status_question(ask)
    assert fcs.maybe_handle_online_status_question(ask) is None


def test_another_subject_served_is_not_the_status_answer(machine, chat, monkeypatch) -> None:
    _state(monkeypatch, local=True, cloud=False, web=False)
    c = chat(machine["desktop"])
    reply = _say(c, "is my friend online")
    assert "Web lookup:" not in reply and "Replies:" not in reply, reply


def test_off_windows_nothing_changes(machine, chat, monkeypatch) -> None:
    _state(monkeypatch, local=True, cloud=False, web=True)
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    for ask in ASKS:
        assert fcs.maybe_handle_online_status_question(ask) is None
    c = chat(machine["desktop"])
    reply = _say(c, "are you online?")
    assert "Web lookup:" not in reply and "Replies:" not in reply, reply


def test_an_unreadable_local_only_state_is_reported_as_local_only(machine, monkeypatch) -> None:
    """SA: if the Local Only reader fails, the answer promises less: no cloud."""
    from core import auto_local_only_mode
    from core.agent_runtime import fast_command_surface

    def broken(_context=None):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(auto_local_only_mode, "turn_is_local_only", broken)
    assert fast_command_surface.online_status_snapshot({})["local_only"] is True
