"""Windows, Steve round 20: "which project am I in?" in a General chat is answered with where the chat is.

Steve, Windows, build 37fcccb4: in a General chat, "which project am I in?" got "Which specific project are
you referring to? Did you mean: project?" 3 of 3 times (route ambiguity_clarification_ask). The answer is
the workspace-identity lane's ("This chat is in General, not in a project."), which runs in the front door.
The entity-ambiguity probe (`VoolAgent._maybe_clarify_ambiguous_entity`) runs before the front door, and
the question passed its eligibility gate (`core.entity_ambiguity.single_plain_know_question`), so the
probe's model claimed it. The gate now leaves the chat's own identity questions to that lane on Windows,
as it does the memory lane's "what is my <X>?" (consistency row 1).

Driven through the served chat as the VOOL window sends it, with the probe stubbed eligible and saying
"ambiguous" (the fixture of tests/test_windows_consistency_row1_memory_ask_before_ambiguity_probe.py), so
the probe would claim the turn if it were asked.
"""
from __future__ import annotations

import itertools

import pytest

from core import windows_write_gate as gate
from core.entity_ambiguity import single_plain_know_question
from tests.test_windows_consistency_row1_memory_ask_before_ambiguity_probe import probe
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

PROJECT_ASK = '{"ambiguous": true, "referents": ["project"], "clarification": "Which specific project are you referring to?"}'
IDENTITY_ASKS = [
    "which project am I in?",
    "Which project am I in",
    "what project is this?",
    "what project are we in?",
    "what folder are you in?",
    "what folder am I in?",
    "which workspace is this chat in?",
]


@pytest.mark.parametrize("ask", IDENTITY_ASKS)
def test_an_identity_ask_gets_the_workspace_answer_with_a_probe_that_would_ask_back(machine, chat, probe, ask) -> None:
    probe["replies"] = itertools.cycle([PROJECT_ASK])
    replies = [chat(machine["desktop"]).say(ask, turn_id=f"r20-{run}") for run in range(3)]

    assert probe["calls"] == [], "the ambiguity probe was asked about the chat's own place"
    assert len(set(replies)) == 1, replies
    assert "Which specific project" not in replies[0] and "Did you mean" not in replies[0], replies[0]


def test_in_a_general_chat_the_answer_says_general(machine, chat, probe) -> None:
    probe["replies"] = itertools.cycle([PROJECT_ASK])
    talk = chat(machine["desktop"])

    reply = talk.say("which project am I in?", turn_id="r20-general")

    assert "This chat is in General, not in a project." in reply, reply
    assert talk.model_rounds == 0 and probe["calls"] == [], reply


@pytest.mark.parametrize("ask", IDENTITY_ASKS)
def test_the_identity_shape_is_not_probe_eligible_on_windows(machine, ask) -> None:
    assert not single_plain_know_question(ask)


@pytest.mark.parametrize(
    "ask",
    ["tell me about Mercury", "which project should I start?", "what is this project about?", "tell me about project management"],
)
def test_other_questions_keep_their_eligibility_on_windows(monkeypatch, ask) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    before = single_plain_know_question(ask)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert single_plain_know_question(ask) == before


def test_off_windows_the_gate_is_unchanged(monkeypatch) -> None:
    from core.agent_runtime.fast_paths_utility import is_workspace_identity_question

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert is_workspace_identity_question("which project am I in?")
    # The gate reads it only on Windows: off Windows the eligibility is what it was before this change.
    before = single_plain_know_question("which project am I in?")
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert not single_plain_know_question("which project am I in?")
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert single_plain_know_question("which project am I in?") == before
