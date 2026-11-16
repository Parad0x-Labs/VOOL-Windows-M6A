"""0.7.3 carry item 4: a reply that promises work no tool ran is reworded, on every host.

0.7.2 hardware check (Steve, 14 Nov, session openclaw:ae751bc9bc84ccd1a4b9): "trades for BONK", with no
`$` anchor, seated no tool. The model answered "Let me pull the data 🚀", the turn ended Done with nothing
run, and its honesty receipt was signed `no_action_claimed`.

Served through /api/chat as the VOOL window sends it (the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py), with the model stubbed to answer in prose and call
no tool:

- a reply that ends on a promise of work ("Let me pull the data 🚀", "One moment while I ...") is replaced
  by the runtime's honest "Nothing is running right now ..." reply, and the receipt records the block;
- a reply with real content before the promise keeps the content and says nothing ran;
- a normal reply and an offer ("Let me know if ...", "Want me to fetch ...?", "If you want, I'll ...") are
  unchanged; at the validator, so are the runtime's "Ask again and I'll fetch ..." and an answer that
  follows "Let me check:";
- the same on Windows and off it (the item is all hosts).
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.agent_runtime import action_honesty_validator as validator
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine  # the served-chat fixtures

HONEST = validator._HONEST_NO_ACTIVE_TASK_RESPONSE
NOTE = validator._UNBACKED_WORK_PROMISE_NOTE


def _served(chat, machine, monkeypatch, *, windows: bool, model_says: str) -> tuple[str, Any]:
    """One served turn of "trades for BONK" whose model answers `model_says` with no tool call."""
    from core.memory_first_router import ModelExecutionDecision

    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    talk = chat(machine["desktop"], model_says=model_says)

    def resolve(**_kwargs: Any) -> ModelExecutionDecision:
        talk.model_rounds += 1
        return ModelExecutionDecision(
            source="model", task_hash=f"t{talk.model_rounds}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", output_text=model_says,
            confidence=0.9, trust_score=0.9, used_model=True,
        )

    monkeypatch.setattr(talk.agent.memory_router, "resolve", resolve)
    reply = talk.say("trades for BONK", turn_id=f"turn-{uuid.uuid4().hex[:10]}")
    assert talk.model_rounds >= 1, "the stubbed model was never asked"
    return reply, talk


def _verdicts(talk) -> list[str]:
    from core.honesty_receipt import list_honesty_receipts

    return [str(r.get("verdict") or "") for r in list_honesty_receipts(talk.session)]


HOSTS = pytest.mark.parametrize("windows", [True, False], ids=["windows", "mac-linux"])


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch):
    """The guard is Windows-only (`windows_write_gate`); a row that drives another host says so."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


@pytest.mark.parametrize(
    "promise",
    ["Let me pull the data 🚀", "On it! I'll grab the latest BONK trades.", "One moment while I gather the trades."],
)
def test_a_bare_promise_with_no_tool_run_gets_the_honest_reply(chat, machine, monkeypatch, promise) -> None:
    reply, talk = _served(chat, machine, monkeypatch, windows=True, model_says=promise)
    assert reply == HONEST, reply
    verdicts = _verdicts(talk)
    assert verdicts and verdicts[-1] == "blocked_false_claim", verdicts


def test_content_before_the_promise_stays_and_says_nothing_ran(chat, machine, monkeypatch) -> None:
    said = "BONK is a Solana memecoin with a large community. I'll grab the latest trades now."
    reply, _talk = _served(chat, machine, monkeypatch, windows=True, model_says=said)
    assert reply == f"BONK is a Solana memecoin with a large community.\n\n{NOTE}", reply


@HOSTS
@pytest.mark.parametrize(
    "said",
    [
        "BONK is a Solana memecoin. Let me know if you want more.",
        "Want me to fetch the latest BONK trades?",
        "If you want, I'll pull the last day of BONK trades.",
        "BONK trades mostly on Raydium and Jupiter.",
    ],
    ids=["let-me-know", "question", "if-you-want", "plain"],
)
def test_normal_replies_and_offers_are_unchanged(chat, machine, monkeypatch, windows, said) -> None:
    reply, talk = _served(chat, machine, monkeypatch, windows=windows, model_says=said)
    assert reply == said, reply
    assert "blocked_false_claim" not in _verdicts(talk)


def test_a_turn_with_a_tool_record_keeps_its_promise(monkeypatch) -> None:
    """At the validator: when the turn has any tool record the promise is not judged."""
    said = "Let me pull the data 🚀"
    monkeypatch.setattr(validator, "_turn_ran_any_tool", lambda **_kwargs: True)
    out = validator._strip_unbacked_work_promises({"response": said}, session_id="s", source_context={})
    assert out["response"] == said and "action_honesty_validator" not in out
    monkeypatch.setattr(validator, "_turn_ran_any_tool", lambda **_kwargs: False)
    out = validator._strip_unbacked_work_promises({"response": said}, session_id="s", source_context={})
    assert out["response"] == HONEST
    assert out["action_honesty_validator"]["reason"] == "unbacked_work_promise_removed"


@pytest.mark.parametrize(
    "said",
    [
        "I can't see live trades from here. Ask again and I'll fetch the real data first.",
        "Name the tokens you want and I'll fetch the trades for each.",
        "Let me check: BONK trades mostly on Raydium.",
        "I'll check the price next time you ask.",
        HONEST,
    ],
    ids=["ask-again", "name-and-ill", "inline-check", "later-step", "the-honest-reply-itself"],
)
def test_offers_and_inline_answers_are_not_promises(monkeypatch, said) -> None:
    """At the validator: the runtime's own offers, an answer after "Let me check:", a later step, and the
    honest reply this guard writes are kept as they are."""
    monkeypatch.setattr(validator, "_turn_ran_any_tool", lambda **_kwargs: False)
    out = validator._strip_unbacked_work_promises({"response": said}, session_id="s", source_context={})
    assert out["response"] == said and "action_honesty_validator" not in out


def test_off_windows_the_promise_reply_is_left_as_mac_ships_it(chat, machine, monkeypatch) -> None:
    said = "Let me pull the data 🚀"
    reply, talk = _served(chat, machine, monkeypatch, windows=False, model_says=said)
    assert reply == said, reply
    monkeypatch.setattr(validator, "_turn_ran_any_tool", lambda **_kwargs: False)
    out = validator._strip_unbacked_work_promises({"response": said}, session_id="s", source_context={})
    assert out == {"response": said}
