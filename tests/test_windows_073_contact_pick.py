"""0.7.3, Steve row 38 on 9e1eb4b4: the person's answer to a wallet "which one?" proposes the contact they named.

"send Testy 1 USDT" with two saved contacts, Testy and Testy R072, asked which one, once. The person answered "Testy R072"
and the model replied "Got it — Testy R072. (Nothing ran on this turn: no tool was called.)": no proposal and no card.
On Windows (core/agent_runtime/fast_paths_contact_pick.py, core/runtime_execution_tools.py
_remember_pending_recipient_choice) the next message naming exactly one offered contact by its full name or one of its
full addresses proposes the original request to that contact; the proposal waits on its approval card. A prefix, a
message naming none or both, a new amount, an expired or used question and every off-Windows turn are handled as before.

SIMULATED CHAIN: the loopback Solana node answering as Mainnet (no real funds). Custody, proposals and the chat are real.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from core import runtime_execution_tools as rt
from core import windows_write_gate as gate
from core.agent_runtime import fast_paths_contact_pick as pick
from tests.test_windows_073_contact_ambiguity import OTHER, _by_name, _script
from tests.test_windows_073_item6_usdt_chat_routing import _pending
from tests.test_windows_073_usdt_explicit_address import SOLANA_MAINNET, STEVE_ROW_4, USDT_MINT, solana_chat
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

pytestmark = [pytest.mark.safety]

#: fixtures the served rows use, imported by name
_FIXTURES = (solana_chat, chat, machine)
ASKED = "2 saved contacts match 'Testy'"
WAITING = "The payment is waiting for your approval: 1 USDT to "


@pytest.fixture(autouse=True)
def _nothing_held():
    rt._RECIPIENT_QUESTIONS.clear()
    rt._PENDING_RECIPIENT_CHOICES.clear()
    yield
    rt._RECIPIENT_QUESTIONS.clear()
    rt._PENDING_RECIPIENT_CHOICES.clear()


@pytest.fixture
def steves_testys(solana_chat):
    """Steve's two contacts: Testy (row 4's address) and Testy R072."""
    from core.contacts import store

    model, node = solana_chat
    node.fund_sol(OTHER, 5_000_000)
    node.open_token_account(OTHER, USDT_MINT)
    store.create_contact(display_name="Testy", actor=store.ACTOR_OWNER,
                         endpoints=[{"kind": "wallet", "value": STEVE_ROW_4, "network": SOLANA_MAINNET}])
    store.create_contact(display_name="Testy R072", actor=store.ACTOR_OWNER,
                         endpoints=[{"kind": "wallet", "value": OTHER, "network": SOLANA_MAINNET}])
    return model


def _say(model: Any, text: str) -> str:
    model.rounds.clear()
    return model.session.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


def _asked(model: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _script(model, monkeypatch, [_by_name()])
    reply = _say(model, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous"], seen
    assert ASKED in reply, reply


def _answer(model: Any, monkeypatch: pytest.MonkeyPatch, text: str) -> tuple[str, list]:
    """The person's next message; the stand-in model, if it is asked at all, answers in prose with no tool call."""
    seen = _script(model, monkeypatch, [])
    return _say(model, text), seen


# --- served: the pick proposes --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("answer", "address"), (
    ("Testy R072", OTHER),
    ("testy r072", OTHER),
    ("Testy R072 please", OTHER),
    ("the second one, Testy R072", OTHER),
    ("Testy", STEVE_ROW_4),  # an exact name is that contact, never the longer name it starts
    (OTHER, OTHER),
    (f"send it to {STEVE_ROW_4}", STEVE_ROW_4),
))
def test_on_windows_naming_one_offered_contact_proposes_it_on_its_card(steves_testys, monkeypatch, answer, address) -> None:
    _asked(steves_testys, monkeypatch)
    reply, seen = _answer(steves_testys, monkeypatch, answer)
    (proposal,) = _pending()
    assert proposal.destination == address
    assert proposal.asset == "USDT"
    assert reply.startswith(WAITING) and "Nothing has been signed or sent yet." in reply, reply
    assert steves_testys.rounds == [], "the model was asked instead of the pick proposing"
    # the one wallet call of the turn is the lane's own, by the picked contact's id, and it waits on the card
    assert [(destination.startswith("ct-"), status) for destination, _t, _s, status in seen] == [(True, "pending_approval")], seen


def test_on_windows_the_pick_answers_one_message_only(steves_testys, monkeypatch) -> None:
    _asked(steves_testys, monkeypatch)
    _answer(steves_testys, monkeypatch, "Testy R072")
    assert len(_pending()) == 1
    _answer(steves_testys, monkeypatch, "Testy R072")
    assert len(_pending()) == 1, "a second message proposed again"


@pytest.mark.parametrize("answer", (
    "Test",  # a prefix of both
    "Testy R07",  # a prefix of one name names the other
    "Testy or Testy R072?",  # both
    "Bob",  # none
    "Testy R072 5 USDT",  # a new amount is a new request
    STEVE_ROW_4[:20],  # part of an address
    "what were the two contacts called again, I think it was Testy R072 but I am not sure",  # more than a pick
))
def test_on_windows_anything_but_one_full_pick_is_handled_as_before(steves_testys, monkeypatch, answer) -> None:
    _asked(steves_testys, monkeypatch)
    _answer(steves_testys, monkeypatch, answer)
    assert _pending() == []
    assert rt._PENDING_RECIPIENT_CHOICES == {}, "the question was not cleared by the answer it did not take"


def test_on_windows_an_expired_question_proposes_nothing(steves_testys, monkeypatch) -> None:
    _asked(steves_testys, monkeypatch)
    later = rt.time.monotonic() + rt._RECIPIENT_QUESTION_TTL_SECONDS + 1
    monkeypatch.setattr(rt.time, "monotonic", lambda: later)
    _answer(steves_testys, monkeypatch, "Testy R072")
    assert _pending() == []
    assert rt._PENDING_RECIPIENT_CHOICES == {}


def test_on_windows_a_frozen_wallet_gets_the_freeze_reply_and_no_card(steves_testys, monkeypatch) -> None:
    from core.wallet import limits

    _asked(steves_testys, monkeypatch)
    limits.set_frozen(True)
    try:
        reply, _seen = _answer(steves_testys, monkeypatch, "Testy R072")
    finally:
        limits.set_frozen(False)
    assert reply.startswith("Payments are frozen (panic freeze is on), so it was not sent. Type /unfreeze to lift it."), reply
    assert _pending() == []


def test_on_windows_the_pick_never_signs_or_sends(steves_testys, monkeypatch) -> None:
    from core.wallet import proposals

    _asked(steves_testys, monkeypatch)
    _answer(steves_testys, monkeypatch, "Testy R072")
    (proposal,) = proposals.list_proposals()
    assert proposal.state == proposals.STATE_PENDING_APPROVAL
    assert not getattr(proposal, "signature", None) and not getattr(proposal, "tx_signature", None)


def test_on_windows_a_proposal_later_in_the_asking_turn_clears_the_question(steves_testys, monkeypatch) -> None:
    from tests.test_windows_073_contact_ambiguity import STRANGER, _by_address

    seen = _script(steves_testys, monkeypatch, [_by_name(), _by_address(STRANGER)])
    _say(steves_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous", "pending_approval"], seen
    assert rt._PENDING_RECIPIENT_CHOICES == {}
    _answer(steves_testys, monkeypatch, "Testy R072")
    assert [p.destination for p in _pending()] == [STRANGER]


# --- units ----------------------------------------------------------------------------------------------------------

_CANDIDATES = (
    {"contact_id": "ct-testy", "display_name": "Testy", "values": STEVE_ROW_4},
    {"contact_id": "ct-r072", "display_name": "Testy R072", "values": f"{OTHER}\x1f0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"},
)


@pytest.mark.parametrize(("text", "picked"), (
    ("Testy R072", "ct-r072"),
    ("TESTY", "ct-testy"),
    ("Testy.", "ct-testy"),
    ("0x5AAEB6053F3E94C9B9A09F33669435E7EF1BEAED", "ct-r072"),
    (STEVE_ROW_4.lower(), None),  # a base58 address is exact, case and all
    ("Testyx", None),
    ("Testy-R072", None),
    ("Testy and Testy R072", None),
    ("", None),
))
def test_the_pick_is_one_full_name_or_address(text, picked) -> None:
    found = pick.picked_candidate(text, _CANDIDATES)
    assert (found or {}).get("contact_id") == picked


def test_on_windows_the_asking_turn_cannot_read_its_own_question(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    context = {"turn_id": "turn-1", "session_id": "s1"}
    rt._remember_pending_recipient_choice({"choices": [{"contact_id": "ct-a", "display_name": "A"},
                                                       {"contact_id": "ct-b", "display_name": "B"}]},
                                          {"asset": "USDT", "amount": "1"}, context)
    assert rt.take_pending_recipient_choice(context) is None
    assert rt.take_pending_recipient_choice({"turn_id": "turn-2", "session_id": "s2"}) is None
    request, candidates = rt.take_pending_recipient_choice({"turn_id": "turn-2", "session_id": "s1"})
    assert request == {"asset": "USDT", "amount": "1"} and [c["contact_id"] for c in candidates] == ["ct-a", "ct-b"]
    assert rt.take_pending_recipient_choice({"turn_id": "turn-3", "session_id": "s1"}) is None


def test_off_windows_no_question_is_kept_and_the_lane_never_answers(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    context = {"turn_id": "turn-1", "session_id": "s1"}
    rt._remember_pending_recipient_choice({"choices": [{"contact_id": "ct-a", "display_name": "A"},
                                                       {"contact_id": "ct-b", "display_name": "B"}]},
                                          {"asset": "USDT", "amount": "1"}, context)
    assert rt._PENDING_RECIPIENT_CHOICES == {}

    class _Agent:
        def _fast_path_result(self, **_kwargs):  # pragma: no cover - reaching it is the bug
            raise AssertionError("the lane answered off Windows")

    assert pick.maybe_handle_contact_pick(_Agent(), "A", session_id="s1", source_context={"turn_id": "turn-2"}) is None
