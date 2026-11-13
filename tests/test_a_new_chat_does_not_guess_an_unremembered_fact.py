"""A new chat asked "what is my test colour?" says it does not know, instead of guessing.

Memory is chat-scoped by design: "remember my test colour is teal" is a chat fact, so another chat
cannot see it. Measured: in a brand-new chat the turn reached the model with nothing to read, and
the model invented an answer ("VOOL Blue"). The memory lane now answers that narrow shape with a
fixed line and no model, ONLY when the chat has no earlier turns and active memory does not hold
the subject. Everything else keeps reaching the model.
"""
from __future__ import annotations

from unittest import mock

import pytest

from core.persistent_memory import (
    _unremembered_subject_line,
    _unremembered_subject_question,
    maybe_handle_memory_command,
)
from core.response_provenance import read_turn_provenance

FIXED_LINE = _unremembered_subject_line("test colour")


@pytest.fixture
def router(tmp_path, monkeypatch):
    from apps.vool_agent import VoolAgent
    from core import runtime_paths, task_state_machine, trace_id
    from core.runtime_continuity import configure_runtime_continuity_db_path
    from core.runtime_provider_defaults import apply_product_runtime_defaults
    from storage.db import configure_default_db_path, reset_default_connection
    from storage.migrations import run_migrations

    home = tmp_path / "fresh-home"
    home.mkdir()
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(home / "workspace"))
    monkeypatch.setenv("VOOL_LOCAL_MODELS_ENABLED", "0")
    monkeypatch.setattr(runtime_paths, "_VOOL_HOME_OVERRIDE", home)
    configure_default_db_path(str(home / "data" / "router.db"))
    reset_default_connection()
    configure_runtime_continuity_db_path(str(home / "data" / "router.db"))
    run_migrations()
    apply_product_runtime_defaults()
    monkeypatch.setattr(trace_id, "_TABLE_READY", False)
    monkeypatch.setattr(task_state_machine, "_TABLE_READY", False)

    def turn(text, chat, *, model=None):
        agent = VoolAgent("test-unremembered", "test-device")
        if model is None:
            return agent.run_once(
                text, session_id_override=chat,
                source_context={"surface": "chat", "chat_id": chat, "runtime_home": str(home)},
            )
        from core.curiosity_roamer import CuriosityResult

        agent.memory_router.resolve = mock.Mock(side_effect=model)
        agent.curiosity.maybe_roam = mock.Mock(return_value=CuriosityResult(enabled=False, mode="off", reason="test"))
        with mock.patch.object(agent, "_live_info_mode", return_value=""), \
                mock.patch.object(agent, "_maybe_execute_model_tool_intent", return_value=None), \
                mock.patch("core.agent_runtime.agent.orchestrate_parent_task", return_value=None):
            return agent.run_once(
                text, session_id_override=chat,
                source_context={"surface": "chat", "chat_id": chat, "runtime_home": str(home)},
            )

    yield turn
    reset_default_connection()
    configure_default_db_path(None)
    configure_runtime_continuity_db_path(None)


def test_a_new_chat_gets_the_fixed_line_without_the_model(router) -> None:
    stored = router("Remember that my test colour is teal.", "unremembered-source")
    assert "Locked in" in stored["response"], stored

    fresh = router("What is my test colour?", "unremembered-fresh")

    assert fresh["response"] == FIXED_LINE, fresh
    assert read_turn_provenance(fresh).model_ran is False, fresh
    assert fresh.get("fast_path_hit") is True, fresh
    assert "teal" not in fresh["response"].lower()


def test_the_chat_that_stored_the_fact_still_gets_teal(router) -> None:
    assert "Locked in" in router("Remember that my test colour is teal.", "unremembered-same")["response"]

    seen: list[str] = []

    def model_reading_its_context(**kwargs):
        """A stand-in model: it answers teal only if teal reached the context it was handed."""
        from core.memory_first_router import ModelExecutionDecision

        context_result = kwargs.get("context_result")
        grounding = " ".join(
            (
                repr((kwargs.get("source_context") or {}).get("conversation_history")),
                str(context_result.assembled_context() if context_result is not None else ""),
            )
        )
        seen.append(grounding)
        answer = "Your test colour is teal." if "teal" in grounding.lower() else "I am not sure."
        return ModelExecutionDecision(
            source="provider_execution", task_hash="unremembered-same", used_model=True,
            provider_id="ollama-local:test", provider_name="ollama-local", model_name="test",
            output_text=answer, confidence=0.7, trust_score=0.75, validation_state="valid",
        )

    asked = router("What is my test colour?", "unremembered-same", model=model_reading_its_context)

    assert asked["response"] != FIXED_LINE, asked
    assert seen, {key: asked.get(key) for key in ("route", "route_reason", "mode")}
    assert "teal" in seen[-1].lower()
    assert "teal" in asked["response"].lower(), asked


def test_active_memory_holding_the_subject_is_never_reported_absent(router) -> None:
    """Even with no transcript, a subject the chat's memory holds is not claimed absent."""
    from core.context_scope import ContextAccessPolicy

    chat = "unremembered-stocked"
    assert "Locked in" in router("Remember that my test colour is teal.", chat)["response"]
    ContextAccessPolicy.for_request(session_id=chat, source_context={"surface": "chat"})

    claimed, response = maybe_handle_memory_command(
        "What is my test colour?",
        session_id=chat,
        source_context={"surface": "chat", "conversation_history": []},
    )

    assert response != FIXED_LINE
    if claimed:
        assert "teal" in response.lower()


@pytest.mark.parametrize(
    "prompt",
    (
        "What is my best option?",
        "What is my repo name?",
        "what is my project name?",
        "What is my name?",
        "What is my first name?",
        "What is my phone number?",
        "What is my colour?",
        "What is my number?",
        "What is my code?",
        "What is my favourite colour and why do you think so?",
        "Tell me what my favourite colour should be.",
        "What is my test colour in hex?",
        "What is my favourite way to deploy?",
    ),
)
def test_controls_are_not_claimed(router, prompt) -> None:
    assert _unremembered_subject_question(prompt) == ""
    claimed, _response = maybe_handle_memory_command(
        prompt,
        session_id="unremembered-controls",
        source_context={"surface": "chat", "conversation_history": [{"role": "user", "content": prompt}]},
    )
    assert claimed is False, prompt


@pytest.mark.parametrize(
    ("prompt", "subject"),
    (
        ("What is my test colour?", "test colour"),
        ("what's my favourite color", "favourite color"),
        ("What was my lucky number?", "lucky number"),
        ("What is my pet's name?", "pet's name"),
        ("What is my favorite animal?", "favorite animal"),
        ("What is my password hint?", "password hint"),
    ),
)
def test_the_narrow_shape_is_recognised(prompt, subject) -> None:
    assert _unremembered_subject_question(prompt) == subject


def test_a_chat_with_earlier_turns_is_not_claimed(router) -> None:
    claimed, _response = maybe_handle_memory_command(
        "What is my favourite colour?",
        session_id="unremembered-ongoing",
        source_context={
            "surface": "chat",
            "conversation_history": [
                {"role": "user", "content": "My favourite colour is green."},
                {"role": "assistant", "content": "Nice."},
                {"role": "user", "content": "What is my favourite colour?"},
            ],
        },
    )
    assert claimed is False


def test_a_caller_without_a_transcript_is_not_evidence_of_a_new_chat(router) -> None:
    """Direct callers that pass no conversation_history keep today's behaviour (decline)."""
    claimed, _response = maybe_handle_memory_command(
        "What is my favourite colour?",
        session_id="unremembered-no-history",
    )
    assert claimed is False
