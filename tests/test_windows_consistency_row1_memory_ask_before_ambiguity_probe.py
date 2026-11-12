"""Windows, consistency row 1: "what is my test animal?" in a new chat gets the same reply every time.

Steve, Windows, build ec7c3205: three new chats with nothing remembered asked "what is my test animal?".
Run 1 said "I don't have your test animal remembered"; runs 2 and 3 asked "Which specific test animal
are you referring to? Did you mean: test animal?".

Why the replies differed: the entity-ambiguity probe (`VoolAgent._maybe_clarify_ambiguous_entity`) runs
BEFORE the front door, and the memory lane that owns this shape (`maybe_handle_memory_command`, the
"not remembered" line) runs INSIDE the front door. The question passed the probe's eligibility gate
(`core.entity_ambiguity.single_plain_know_question`), so the model's verdict picked the reply: "ambiguous"
served the ask-back, "not ambiguous" let the memory lane answer, and a timed-out probe asked back that the
check did not complete. The gate now leaves that shape to the memory lane on Windows, so no model judgment
stands between the ask and its owner.

Driven through the served chat as the VOOL window sends it (`_Chat.say` posts /api/chat). The probe's
author is stubbed eligible, and its judgment call is stubbed to say "ambiguous" (or to vary from run to
run), so the probe would claim the turn if it were asked.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.entity_ambiguity import single_plain_know_question
from core.persistent_memory import _unremembered_subject_line
from tests.test_windows_run_in_folder_served_end_to_end import _Chat, chat, machine

ASK = "what is my test animal?"
NOT_REMEMBERED = _unremembered_subject_line("test animal")
AMBIGUOUS = (
    '{"ambiguous": true, "referents": ["test animal"], '
    '"clarification": "Which specific test animal are you referring to?"}'
)
NOT_AMBIGUOUS = '{"ambiguous": false, "referents": [], "clarification": ""}'


def _raise(_verdict: str) -> str:
    raise TimeoutError("probe deadline")


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An eligible probe author whose judgment comes from ``state["replies"]`` (cycled), counted per call."""
    state: dict[str, Any] = {"calls": [], "replies": itertools.cycle([AMBIGUOUS])}
    monkeypatch.setattr(
        "core.agent_runtime.audit_routing.select_audit_manifests",
        lambda agent, context, routing: (
            [SimpleNamespace(provider_id="ollama-local:qwen3:8b", metadata={"cost_class": "free_local"})], ""
        ),
    )
    monkeypatch.setattr("core.agent_runtime.audit_routing.resolve_routing_mode", lambda context: "auto")
    monkeypatch.setattr(
        "core.final_answer_authorship.decide_final_answer_author",
        lambda **_: SimpleNamespace(eligible=True, reason="stub"),
    )

    def ask(system_prompt: str, prompt: str) -> str:
        state["calls"].append(prompt)
        reply = next(state["replies"])
        return reply("") if callable(reply) else reply

    monkeypatch.setattr(
        "core.agent_runtime.turn_planner_hook.build_conductor_ask_model",
        lambda agent, context, **kwargs: ask,
    )
    return state


def _new_chat(chat, machine) -> _Chat:
    return chat(machine["desktop"])


def test_a_new_chat_says_not_remembered_every_time_with_a_probe_that_would_ask_back(
    machine, chat, probe
) -> None:
    replies = []
    for run in range(5):
        session = _new_chat(chat, machine)
        replies.append(session.say(ASK, turn_id=f"row1-{run}"))
        assert session.model_rounds == 0, replies[-1]
    assert replies == [NOT_REMEMBERED] * 5, replies
    assert probe["calls"] == [], "the ambiguity probe was asked about a memory question"


def test_the_reply_does_not_follow_the_probe_verdict(machine, chat, probe) -> None:
    """The three verdicts the probe gave in the field (ambiguous, not ambiguous, timed out) give one reply."""
    probe["replies"] = itertools.cycle([AMBIGUOUS, NOT_AMBIGUOUS, _raise])
    replies = [_new_chat(chat, machine).say(ASK, turn_id=f"row1-v{run}") for run in range(6)]
    assert set(replies) == {NOT_REMEMBERED}, replies
    assert probe["calls"] == []


def test_a_remembered_test_animal_is_recalled(machine, chat, probe, monkeypatch) -> None:
    """The chat that saved the fact gets it: the turn reaches the model with the fact in what it reads."""
    from core.memory_first_router import ModelExecutionDecision

    session = _new_chat(chat, machine)
    stored = session.say("Remember that my test animal is an otter.", turn_id="row1-store")
    assert "Locked in" in stored, stored
    read: list[str] = []

    def model_reading_its_context(**kwargs: Any) -> ModelExecutionDecision:
        """A stand-in model: it names the otter only if the otter reached what it was handed."""
        read.append(repr(kwargs))
        message = "Your test animal is an otter." if "otter" in read[-1].lower() else "I am not sure."
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"r{len(read)}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test",
            structured_output={"intent": "respond.direct", "arguments": {"message": message}},
            output_text=message, confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )

    monkeypatch.setattr(session.agent.memory_router, "resolve_tool_intent", model_reading_its_context)
    monkeypatch.setattr(session.agent.memory_router, "resolve", model_reading_its_context)
    asked = session.say(ASK, turn_id="row1-recall")
    assert asked != NOT_REMEMBERED, asked
    assert "Which specific test animal" not in asked, asked
    assert "otter" in asked.lower(), asked
    assert probe["calls"] == []


def test_a_genuinely_ambiguous_name_still_gets_the_clarification(machine, chat, probe) -> None:
    probe["replies"] = itertools.cycle([
        '{"ambiguous": true, "referents": ["Mercury (planet)", "Mercury (element)"], '
        '"clarification": "Which Mercury do you mean?"}'
    ])
    reply = _new_chat(chat, machine).say("tell me about Mercury", turn_id="row1-mercury")
    assert probe["calls"], reply
    assert "Mercury (planet)" in reply and "Mercury (element)" in reply, reply


@pytest.mark.parametrize("prompt", ["What is my test animal?", "what's my favourite colour", "What was my lucky number?"])
def test_the_memory_shape_is_not_probe_eligible_on_windows(monkeypatch: pytest.MonkeyPatch, prompt: str) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert not single_plain_know_question(prompt)


@pytest.mark.parametrize(
    "prompt", ["tell me about Mercury", "What is the population of Springfield?", "What is my best option?"]
)
def test_other_questions_keep_their_eligibility_on_windows(monkeypatch: pytest.MonkeyPatch, prompt: str) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    before = single_plain_know_question(prompt)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert single_plain_know_question(prompt) == before


def test_off_windows_the_gate_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert single_plain_know_question(ASK)

