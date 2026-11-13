"""`cloud test` is answered deterministically, never by the model.

Capability test on Windows: with no key, `cloud test` went to model chat. No parser took it
(`_CLOUD_CMD_RE` has no `test`, `_CONNECTION_TEST_RE` wants a `conn...` word), a non-owner got None,
and the connection test never looked at the Windows cloud gate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.vool_agent import VoolAgent
from core import ollama_cloud_models, runtime_paths, windows_cloud_gate
from core.agent_runtime.fast_command_surface import maybe_handle_connection_test_intent

VARIANTS = ("cloud test", "/cloud test", "  Cloud  Test  ")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


@pytest.fixture()
def no_probe(monkeypatch):
    calls: list[int] = []

    def _probe(*_a, **_k):
        calls.append(1)
        raise AssertionError("the auth probe must not run")

    monkeypatch.setattr("core.cloud_connection_state.run_auth_probe", _probe)
    return calls


@pytest.fixture()
def windows_cloud_off(monkeypatch):
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", True)
    monkeypatch.setattr(windows_cloud_gate, "cloud_key_stored", lambda: False)
    monkeypatch.setattr(windows_cloud_gate, "owner_turned_cloud_on", lambda: False)


@pytest.fixture()
def off_windows(monkeypatch):
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", False)


def _frontdoor(text: str, **context):
    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    result = agent._handle_turn_frontdoor(
        raw_user_input=text,
        effective_input=text,
        normalized_input=text,
        source_surface="api",
        session_id="cloud-test-command",
        source_context={"surface": "api", **context},
        persona=None,
        interpreted=SimpleNamespace(understanding_confidence=0.8, topic_hints=()),
    )
    return (result or {}).get("result")


def test_windows_cloud_off_returns_off_text_without_probe(windows_cloud_off, no_probe):
    for text in VARIANTS:
        action = maybe_handle_connection_test_intent(text, owner_local=True)
        assert action is not None, text
        assert action["response"] == windows_cloud_gate.OFF_TEXT
        assert action["advice_only"] is True and action["success"] is False
    assert no_probe == []


def test_windows_cloud_off_frontdoor_answers_without_model(windows_cloud_off, no_probe):
    reply = _frontdoor("cloud test", _owner_local=True)
    assert reply is not None
    assert reply.get("response") == windows_cloud_gate.OFF_TEXT
    assert no_probe == []


def test_no_key_with_cloud_open_returns_the_no_key_line(off_windows, monkeypatch):
    monkeypatch.setattr("core.credential_store.get_credential", lambda name: "")
    action = maybe_handle_connection_test_intent("cloud test", owner_local=True)
    assert action is not None and action["success"] is False
    assert "no cloud key stored" in action["response"]
    reply = _frontdoor("cloud test", _owner_local=True)
    assert reply is not None and "no cloud key stored" in str(reply.get("response"))


def test_plan_mode_is_deterministic_and_does_not_probe(off_windows, no_probe):
    action = maybe_handle_connection_test_intent("cloud test", owner_local=True, read_only=True)
    assert action["response"] == "Switch to Manual to run a cloud test."
    assert action["advice_only"] is True
    reply = _frontdoor("cloud test", _owner_local=True, operating_mode="plan")
    assert reply is not None
    assert reply.get("response") == "Switch to Manual to run a cloud test."
    assert no_probe == []


def test_plan_mode_with_windows_cloud_off_says_cloud_is_off(windows_cloud_off, no_probe):
    reply = _frontdoor("cloud test", _owner_local=True, operating_mode="plan")
    assert reply is not None and reply.get("response") == windows_cloud_gate.OFF_TEXT
    assert no_probe == []


def test_non_owner_gets_a_fixed_line(off_windows, no_probe, monkeypatch):
    monkeypatch.setattr("core.credential_store.get_credential", lambda name: "sk-or-v1-" + "f" * 56)
    action = maybe_handle_connection_test_intent("cloud test", owner_local=False)
    assert action is not None and action["advice_only"] is True
    assert "owner's own local session" in action["response"]
    reply = _frontdoor("cloud test", _owner_local=False)
    assert reply is not None and "owner's own local session" in str(reply.get("response"))
    assert no_probe == []


def test_non_owner_line_does_not_reveal_cloud_state(windows_cloud_off, no_probe):
    action = maybe_handle_connection_test_intent("cloud test", owner_local=False)
    assert windows_cloud_gate.OFF_TEXT not in action["response"]


def test_natural_phrasing_keeps_working(off_windows, monkeypatch):
    monkeypatch.setattr("core.credential_store.get_credential", lambda name: "")
    action = maybe_handle_connection_test_intent("test the connection to openrouter", owner_local=True)
    assert action is not None and "no cloud key stored" in action["response"]
    reply = _frontdoor("test the connection to openrouter", _owner_local=True)
    assert reply is not None and "no cloud key stored" in str(reply.get("response"))


def test_other_cloud_text_is_not_claimed():
    for text in ("cloud testing ideas", "cloud test please run it now", "test cloud", "cloud tests"):
        assert maybe_handle_connection_test_intent(text, owner_local=True) is None, text
