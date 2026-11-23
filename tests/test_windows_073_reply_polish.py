"""0.7.3, Steve on 725f22b2: an honest "the payment was not sent" refusal is not a false payment claim on Windows.

Steve sent to a raw address with the panic freeze on (receipt pay-caa03545, fault wallet_limit_exceeded). The tool said
"Payments are frozen (panic freeze is on), so it was not sent." and the model's draft said so too, but the reply was
swapped for the generic wallet-safety text (route false_payment_claim_blocked), so the person never saw the freeze.
core/agent_runtime/action_honesty_validator.py _claims_false_payment looked for a negation only BEFORE the claim match,
and the claim pattern lets a few words sit between "payment" and "sent", so in "the payment was not sent" the "not" was
inside the match. On Windows a negation inside the match that governs the verb is an honest report. A real claim still
trips, and off Windows the detector answers as Mac ships it.
"""
from __future__ import annotations

import pytest

from core import windows_write_gate as gate
from core.agent_runtime import action_honesty_validator as v
from core.agent_runtime import research_tool_loop_facade as loop
from tests.test_windows_073_contact_ambiguity import QUESTION, _by_address, _by_name, _script, two_testys
from tests.test_windows_073_item6_usdt_chat_routing import _pending
from tests.test_windows_073_usdt_explicit_address import STEVE_ROW_4, solana_chat
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

#: fixtures the served rows use, imported by name
_FIXTURES = (two_testys, solana_chat, chat, machine)

pytestmark = [pytest.mark.safety]

HONEST = (
    "The payment was not sent because payments are frozen (panic freeze is on). Type /unfreeze to lift it.",
    "The payment wasn't sent.",
    "Your payment has not been sent: payments are frozen.",
    "The transaction was never broadcast.",
    "The 1 USDT payment was not sent.",
    "Payment not sent: payments are frozen.",
    "The transfer hasn't gone through.",
    "The payment has not yet been sent.",
)

FALSE_CLAIMS = (
    "The payment was sent.",
    "Your 1 USDT payment has been sent to Testy Ann.",
    "The payment didn't fail and was sent.",
    "The transaction was broadcast and confirmed.",
    "Done! The funds were transferred.",
    "I signed and sent the transaction.",
    "The payment was not delayed and was sent.",
    "The payment you didn't expect was sent.",
    "The payment I never doubted was sent.",
)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)


@pytest.mark.parametrize("text", HONEST)
def test_on_windows_an_honest_not_sent_report_is_not_a_payment_claim(windows, text) -> None:
    assert v._claims_false_payment(text) is False


@pytest.mark.parametrize("text", HONEST)
def test_on_windows_the_freeze_refusal_reaches_the_person(windows, text) -> None:
    output = {"response": text, "confidence": 0.9}
    guarded = v._enforce_wallet_secret_safety(dict(output), session_id="s", source_context={})
    assert guarded["response"] == text
    assert guarded.get("route_reason") != "false_payment_claim_blocked"


@pytest.mark.parametrize("text", FALSE_CLAIMS)
def test_on_windows_a_real_payment_claim_still_trips(windows, text) -> None:
    assert v._claims_false_payment(text) is True
    guarded = v._enforce_wallet_secret_safety({"response": text, "confidence": 0.9}, session_id="s", source_context={})
    assert guarded["route_reason"] == "false_payment_claim_blocked"


@pytest.mark.parametrize("text", HONEST + FALSE_CLAIMS)
def test_off_windows_the_detector_answers_as_mac_ships_it(off_windows, text) -> None:
    assert v._claims_false_payment(text) is True


# --- Row 38 with the which-one question: the question is the reply ---------------------------------------------------


def _then_the_model_fails(model, monkeypatch, calls):
    """The stand-in model makes `calls` (wallet.propose arguments), then its next round comes back with no tool call."""
    from core.memory_first_router import ModelExecutionDecision
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)  # the follow-up-failed seam (Windows)
    seen = _script(model, monkeypatch, [])
    rounds: list[int] = []

    def decide(**_kwargs):
        rounds.append(1)
        if len(rounds) <= len(calls):
            output, used, details = {"intent": "wallet.propose", "arguments": dict(calls[len(rounds) - 1])}, True, {}
        else:
            output, used, details = {}, False, {"block_reason": "all_ranked_providers_failed"}
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"t{len(rounds)}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=output, confidence=0.8, trust_score=0.84,
            used_model=used, validation_state="valid" if used else "failed", details=details,
        )

    monkeypatch.setattr(model.session.agent.memory_router, "resolve_tool_intent", decide)
    return seen, rounds


@pytest.mark.parametrize(
    "calls",
    (
        (_by_name(),),  # Steve, row 38 on 725f22b2 with qwen3:8b: the round after the question had no tool call
        (_by_name(), _by_address(STEVE_ROW_4)),  # ...and with the model's same-turn pick held first
    ),
)
def test_on_windows_a_failed_follow_up_after_a_which_one_answer_shows_the_question_once(two_testys, monkeypatch, calls) -> None:
    seen, rounds = _then_the_model_fails(two_testys, monkeypatch, calls)
    reply = two_testys.session.say("send Testy 1 USDT", turn_id="turn-row38-polish")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous"] * len(calls), seen
    assert len(rounds) == len(calls) + 1, rounds
    assert reply.count(QUESTION) == 1 and reply.count("Which one?") == 1, reply
    assert "follow-up step failed" not in reply, reply
    assert _pending() == []


_QUESTION_STEP = {"tool_name": "wallet.propose", "ok": True, "status": "recipient_ambiguous",
                  "response_text": "2 saved contacts match 'Testy'. Which one? Nothing was proposed, signed or sent."}


@pytest.mark.parametrize(
    "step",
    (
        {**_QUESTION_STEP, "ok": False},
        {**_QUESTION_STEP, "status": "pending_approval"},
        {**_QUESTION_STEP, "status": "recipient_not_found"},
        {**_QUESTION_STEP, "tool_name": "contacts.search"},
        {**_QUESTION_STEP, "response_text": "  "},
        None,
    ),
)
def test_on_windows_only_a_wallet_which_one_answer_ends_the_loop(windows, step) -> None:
    assert loop._windows_tool_asked_the_person(_QUESTION_STEP) is True
    assert loop._windows_tool_asked_the_person(step) is False


def test_off_windows_a_which_one_answer_does_not_end_the_loop(off_windows) -> None:
    assert loop._windows_tool_asked_the_person(_QUESTION_STEP) is False
