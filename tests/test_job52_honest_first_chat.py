"""A new user's first chat must be answered honestly (clean-VM job 52, port 76afbd36).

Two first-chat findings on a fresh Windows install:

* #10 -- with no model on the machine, the canned "what can we do" reply promised to "answer
  questions and keep our context, locally and free on your machine", and the very next turn was
  refused because no local model was running.
* #9 -- once a model was loaded, "Hello, what can you do?" came back as "Could not be answered:
  Hello -- the available answer path does not safely match this request". The turn parser split
  the greeting off as its own clause, no head set claims "Hello", so it was an UNKNOWN demand the
  composer had to report as unanswerable. A leading greeting is a salutation, not a request.
"""

from __future__ import annotations

import pytest

import core.local_model_presence as presence
from core.agent_runtime.fast_paths_utility import smalltalk_fast_path
from core.turn_ir import ClauseKind, parse_turn_ir


class _Agent:
    def _help_capabilities_text(self) -> str:
        return "HELP"


def _what_can_we(monkeypatch, running: bool) -> str:
    monkeypatch.setattr(presence, "local_model_running", lambda *a, **k: running)
    reply = smalltalk_fast_path(_Agent(), "so what can we do", source_surface="api", session_id="s")
    assert reply
    return reply


def test_no_local_model_reply_does_not_promise_local_answers(monkeypatch) -> None:
    reply = _what_can_we(monkeypatch, running=False)
    assert "locally and free" not in reply
    assert "no local model" in reply.lower()


def test_with_a_local_model_the_reply_still_offers_local_answers(monkeypatch) -> None:
    reply = _what_can_we(monkeypatch, running=True)
    assert "locally and free on your machine" in reply


@pytest.mark.parametrize(
    ("text", "request_text", "kind"),
    [
        ("Hello, what can you do?", "what can you do?", ClauseKind.KNOW),
        ("Hi! Why is the sky blue?", "Why is the sky blue?", ClauseKind.KNOW),
        ("hey there, why is the sky blue?", "why is the sky blue?", ClauseKind.KNOW),
    ],
)
def test_a_leading_greeting_is_not_its_own_demand(text, request_text, kind) -> None:
    ir = parse_turn_ir(text)
    assert [c.request_text for c in ir.clauses] == [request_text]
    assert ir.clauses[0].kind is kind
    assert ir.clauses[0].ordinal == 1


def test_a_greeting_alone_is_still_the_whole_turn() -> None:
    ir = parse_turn_ir("Hello")
    assert [c.request_text for c in ir.clauses] == ["Hello"]


def test_a_word_that_is_not_a_greeting_keeps_its_clause() -> None:
    ir = parse_turn_ir("Helios, what can you do?")
    assert ir.clauses[0].request_text == "Helios"
    assert len(ir.clauses) == 2
