"""Per-turn autonomy override: 'Auto' mode / the inline Allow controls relax the approval gate.

The composer's Auto mode and the inline "Allow once / Always allow in this chat" buttons send an
`autonomy` the turn puts in force (core.execution_gate). It must relax ROUTINE LOCAL actions without
a prompt, must NOT relax outward-facing / privacy-sensitive actions (safety), and must never leak
past the turn.
"""
import pytest

from apps.vool_agent import VoolAgent
from core import execution_gate as g
from core.turn_contract import TURN_REQUEST_KEY, TurnRequest

G = g.ExecutionGate


def _req(action, **kw):
    return G._requires_explicit_approval(action, **kw)


def test_default_gates_a_routine_local_action():
    # No override -> the persisted preference (never "auto") gates a routine destructive action.
    assert g.effective_autonomy_mode() != "auto"
    assert _req("cleanup_temp_files") is True
    assert _req("read_file") is False


def test_auto_relaxes_local_but_keeps_outward_and_privacy():
    tok = g.set_request_autonomy_override("auto")
    try:
        assert g.effective_autonomy_mode() == "auto"
        assert _req("cleanup_temp_files") is False   # routine local -> runs without asking
        assert _req("move_path") is False
        # Safety: even under "always allow in this chat", these still confirm.
        assert _req("discord_post", outward_facing=True) is True
        assert _req("read_file", privacy_sensitive=True) is True
    finally:
        g.reset_request_autonomy_override(tok)
    assert g.effective_autonomy_mode() != "auto"
    assert _req("cleanup_temp_files") is True         # back to asking after the turn


def test_unknown_override_value_is_ignored():
    tok = g.set_request_autonomy_override("go-bananas")
    try:
        assert g.effective_autonomy_mode() != "auto"  # garbage falls back to the saved preference
    finally:
        g.reset_request_autonomy_override(tok)


def test_run_once_puts_override_in_force_then_resets(monkeypatch):
    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    seen = {}

    def fake_inner(self, user_input, *, session_id_override=None, source_context=None, turn_request):
        assert isinstance(turn_request, TurnRequest)
        assert source_context[TURN_REQUEST_KEY] is turn_request
        assert turn_request.request_id
        assert turn_request.user_text == user_input
        assert turn_request.autonomy_override == "auto"
        seen["during"] = g.effective_autonomy_mode()
        return {"response": "ok"}

    monkeypatch.setattr(VoolAgent, "_run_once_inner", fake_inner)
    agent.run_once("hi", source_context={"autonomy_override": "auto"})
    assert seen["during"] == "auto"
    assert g._REQUEST_AUTONOMY_OVERRIDE.get() is None   # reset in finally


def test_run_once_resets_override_even_on_exception(monkeypatch):
    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")

    def boom(self, user_input, *, session_id_override=None, source_context=None, turn_request):
        assert isinstance(turn_request, TurnRequest)
        assert source_context[TURN_REQUEST_KEY] is turn_request
        assert turn_request.request_id
        assert turn_request.user_text == user_input
        assert turn_request.autonomy_override == "auto"
        assert g.effective_autonomy_mode() == "auto"
        raise RuntimeError("boom")

    monkeypatch.setattr(VoolAgent, "_run_once_inner", boom)
    with pytest.raises(RuntimeError, match=r"^boom$"):
        agent.run_once("hi", source_context={"autonomy_override": "auto"})
    assert g._REQUEST_AUTONOMY_OVERRIDE.get() is None


def test_run_once_does_not_carry_override_into_the_next_canonical_request(monkeypatch):
    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    seen = []

    def fake_inner(self, user_input, *, session_id_override=None, source_context=None, turn_request):
        assert isinstance(turn_request, TurnRequest)
        assert source_context[TURN_REQUEST_KEY] is turn_request
        assert turn_request.user_text == user_input
        seen.append((turn_request, g.effective_autonomy_mode()))
        return {"response": "ok"}

    monkeypatch.setattr(VoolAgent, "_run_once_inner", fake_inner)
    agent.run_once("first local turn", source_context={"autonomy_override": "auto"})
    agent.run_once("a different next turn", source_context={})

    assert seen[0][0].autonomy_override == "auto"
    assert seen[0][1] == "auto"
    assert seen[1][0].autonomy_override == ""
    assert seen[1][1] != "auto"
    assert seen[0][0].request_id != seen[1][0].request_id
    assert g._REQUEST_AUTONOMY_OVERRIDE.get() is None
