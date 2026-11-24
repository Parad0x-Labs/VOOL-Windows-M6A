"""0.7.3, composer retry on 6ca09fed (24 Nov 2026): "send Testy 1 USDT" from the composer reaches its approval card.

Jesse typed "send Testy 1 USDT". The model passed chain=ethereum, network=mainnet. On Windows "mainnet" names the
environment, not a network, so it was dropped, but the chain guessed beside it survived for a contact name. It was kept
with the which-one pick, and the pick then asked Testy, who has only a Solana wallet, for an Ethereum one. The model also
repeated the call in the same turn, the loop ended on the repeat's text "I already asked...", and the question itself was
never shown.

On Windows (core/wallet/transfers.py explicit_address_hints) a chain beside an environment word is dropped for a contact
name or id too, so the resolver keeps the chains that carry the asset and the contact's saved wallet pins its own network.
The held which-one answer (core/runtime_execution_tools.py _still_asked) now repeats the question word for word before its
note. An explicit chain with a real network is kept, and off Windows nothing changes.

SIMULATED CHAIN: the loopback Solana node answering as Mainnet (no real funds). Custody, proposals and the chat are real.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from core import runtime_execution_tools as rt
from core import windows_write_gate as gate
from core.wallet import transfers
from tests.test_windows_073_contact_ambiguity import OTHER, _script
from tests.test_windows_073_item6_usdt_chat_routing import _pending
from tests.test_windows_073_usdt_explicit_address import EVM, SOLANA_MAINNET, STEVE_ROW_4, USDT_MINT, solana_chat
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

pytestmark = [pytest.mark.safety]

#: fixtures the served rows use, imported by name
_FIXTURES = (solana_chat, chat, machine)
ASKED = "2 saved contacts match 'Testy'"
WAITING = "The payment is waiting for your approval: 1 USDT to "
FROZEN = "Payments are frozen (panic freeze is on), so it was not sent. Type /unfreeze to lift it."
#: what the model passed from the composer (Steve's trace, composer-retry-raw.txt)
COMPOSER = {"destination": "Testy", "asset": "USDT", "amount": "1", "chain": "ethereum", "network": "mainnet"}


@pytest.fixture(autouse=True)
def _nothing_held():
    rt._RECIPIENT_QUESTIONS.clear()
    rt._PENDING_RECIPIENT_CHOICES.clear()
    yield
    rt._RECIPIENT_QUESTIONS.clear()
    rt._PENDING_RECIPIENT_CHOICES.clear()


def _contacts(solana_chat, *contacts: tuple[str, list[dict[str, str]]]):
    from core.contacts import store

    model, node = solana_chat
    node.fund_sol(OTHER, 5_000_000)
    node.open_token_account(OTHER, USDT_MINT)
    for name, endpoints in contacts:
        store.create_contact(display_name=name, actor=store.ACTOR_OWNER, endpoints=endpoints)
    return model


def _solana(address: str) -> dict[str, str]:
    return {"kind": "wallet", "value": address, "network": SOLANA_MAINNET}


@pytest.fixture
def solana_only_testy(solana_chat):
    return _contacts(solana_chat, ("Testy", [_solana(STEVE_ROW_4)]))


@pytest.fixture
def steves_testys(solana_chat):
    return _contacts(solana_chat, ("Testy", [_solana(STEVE_ROW_4)]), ("Testy R072", [_solana(OTHER)]))


@pytest.fixture
def testy_on_two_chains(solana_chat):
    return _contacts(solana_chat, ("Testy", [_solana(STEVE_ROW_4), {"kind": "wallet", "value": EVM, "network": "eip155:1"}]))


def _say(model: Any, text: str) -> str:
    model.rounds.clear()
    return model.session.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


# --- A: the chain guessed beside "mainnet" is dropped for a contact name ---------------------------------------------

def test_on_windows_a_solana_only_contact_sent_as_ethereum_mainnet_gets_its_card_on_solana(solana_only_testy, monkeypatch) -> None:
    seen = _script(solana_only_testy, monkeypatch, [COMPOSER])
    _say(solana_only_testy, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["pending_approval"], seen
    (proposal,) = _pending()
    assert (proposal.destination, proposal.network, proposal.asset) == (STEVE_ROW_4, SOLANA_MAINNET, "USDT")


def test_on_windows_the_same_send_while_frozen_gets_the_freeze_text_and_no_card(solana_only_testy) -> None:
    from core.wallet import limits

    limits.set_frozen(True)
    try:
        result = rt._wallet_model_tool("wallet.propose", dict(COMPOSER), {
            "session_id": "frozen", "turn_id": "turn-f", "turn_request": {"user_text": "send Testy 1 USDT"}})
    finally:
        limits.set_frozen(False)
    assert result.response_text.startswith(FROZEN), result.response_text
    assert _pending() == []


def test_on_windows_ambiguity_then_a_pick_of_testy_reaches_the_card(steves_testys, monkeypatch) -> None:
    seen = _script(steves_testys, monkeypatch, [COMPOSER])
    reply = _say(steves_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous"], seen
    assert ASKED in reply, reply
    assert "chain" not in rt._PENDING_RECIPIENT_CHOICES[next(iter(rt._PENDING_RECIPIENT_CHOICES))][2]
    _script(steves_testys, monkeypatch, [])
    reply = _say(steves_testys, "Testy")
    (proposal,) = _pending()
    assert (proposal.destination, proposal.network) == (STEVE_ROW_4, SOLANA_MAINNET)
    assert reply.startswith(WAITING), reply


def test_on_windows_a_contact_with_usdt_on_solana_and_ethereum_is_asked_which_not_picked(testy_on_two_chains, monkeypatch) -> None:
    seen = _script(testy_on_two_chains, monkeypatch, [COMPOSER])
    reply = _say(testy_on_two_chains, "send Testy 1 USDT")
    assert seen and seen[0][3] == "recipient_ambiguous", (seen, reply)
    assert _pending() == []


COMPOSER_TEXT = "send Testy 1 USDT"


@pytest.mark.parametrize(("destination", "chain", "network", "text", "expected"), (
    ("Testy", "ethereum", "mainnet", COMPOSER_TEXT, ("", "")),  # the model's guess beside "mainnet": dropped
    ("Testy", "", "mainnet", COMPOSER_TEXT, ("", "")),
    ("ct-0123456789abcdef", "ethereum", "mainnet", "pay ct-0123456789abcdef 1 USDT", ("", "")),  # a contact id, like a name
    ("Testy", "ethereum", "mainnet", "send Testy 1 USDT on ethereum", ("ethereum", "")),  # the person named it: kept
    ("Testy", "ethereum", "mainnet", "send Testy 1 USDT on Ethereum please", ("ethereum", "")),
    ("Testy", "ethereum", "mainnet", "send Testy 1 USDT (ERC20)", ("ethereum", "")),
    ("Testy", "base", "mainnet", "send Testy 1 USDT on base", ("base", "")),
    ("Testy", "ethereum", "mainnet", "send Testy 1 USDT on sol", ("ethereum", "")),  # any chain named: not dropped
    ("Testy", "ethereum", "mainnet", None, ("ethereum", "")),  # no message to read: kept, fail closed
    ("Testy", "ethereum", "eip155:1", COMPOSER_TEXT, ("ethereum", "eip155:1")),  # an explicit chain with a real network
    ("Testy", "solana", SOLANA_MAINNET, COMPOSER_TEXT, ("solana", SOLANA_MAINNET)),
    ("Testy", "base", "", COMPOSER_TEXT, ("base", "")),  # a chain with no network at all is not a guess beside "mainnet"
    (STEVE_ROW_4, "solana", "mainnet", COMPOSER_TEXT, ("solana", "")),  # an address keeps the chain of its own family
    (EVM, "ethereum", "mainnet", COMPOSER_TEXT, ("ethereum", "")),
))
def test_on_windows_only_a_guessed_chain_beside_an_environment_word_is_dropped_for_a_contact(
        monkeypatch, destination, chain, network, text, expected) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    fitted = transfers.explicit_address_hints(destination, chain=chain, network=network, environment="mainnet",
                                              request_text=text)
    assert ((fitted.chain, fitted.network), fitted.refusal) == (expected, "")


def test_on_windows_send_testy_on_ethereum_to_a_solana_only_contact_is_refused(solana_only_testy, monkeypatch) -> None:
    seen = _script(solana_only_testy, monkeypatch, [COMPOSER])
    reply = _say(solana_only_testy, "send Testy 1 USDT on ethereum")
    assert seen and seen[0][3] == "recipient_has_no_address", (seen, reply)
    assert "Ethereum" in reply and _pending() == [], reply


def test_on_windows_the_pick_keeps_the_chain_the_asking_turn_kept(steves_testys, monkeypatch) -> None:
    seen = _script(steves_testys, monkeypatch, [COMPOSER])
    _say(steves_testys, "send Testy 1 USDT on ethereum")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous"], seen
    ((_until, _turn, request, _candidates),) = rt._PENDING_RECIPIENT_CHOICES.values()
    assert request.get("chain") == "ethereum", request
    _script(steves_testys, monkeypatch, [])
    reply = _say(steves_testys, "Testy")
    assert _pending() == [], reply  # Testy has no Ethereum wallet: refused, never moved to Solana
    assert "Ethereum" in reply, reply


@pytest.mark.parametrize(("destination", "chain", "network"), (
    ("Testy", "ethereum", "mainnet"),  # with the person's message naming no chain, too (below)
    ("Testy", "ethereum", "eip155:1"),
    (STEVE_ROW_4, "ethereum", "mainnet"),
))
def test_off_windows_the_hints_come_back_as_given(monkeypatch, destination, chain, network) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    fitted = transfers.explicit_address_hints(destination, chain=chain, network=network, environment="mainnet",
                                              request_text=COMPOSER_TEXT)
    assert ((fitted.chain, fitted.network), fitted.refusal) == ((chain, network), "")


# --- B: a repeat in the same turn shows the question exactly once ----------------------------------------------------

def test_on_windows_a_two_step_turn_shows_which_one_exactly_once(steves_testys, monkeypatch) -> None:
    # Steve's turn: the composer call, the model's bare repeat, and the repeat again, which the loop blocks
    seen = _script(steves_testys, monkeypatch, [COMPOSER, {"destination": "Testy"}, {"destination": "Testy"}])
    reply = _say(steves_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen][:2] == ["recipient_ambiguous", "recipient_ambiguous"], seen
    assert reply.count("Which one?") == 1 and reply.count(ASKED) == 1, reply
    assert _pending() == []


def test_on_windows_the_held_answer_repeats_the_question_then_its_note(steves_testys, monkeypatch) -> None:
    context = {"session_id": "s-held", "turn_id": "turn-held"}
    first = rt._wallet_model_tool("wallet.propose", dict(COMPOSER), context)
    again = rt._wallet_model_tool("wallet.propose", {"destination": "Testy"}, context)
    assert (first.status, again.status) == ("recipient_ambiguous", "recipient_ambiguous")
    assert again.response_text.startswith(first.response_text), (first.response_text, again.response_text)
    assert again.response_text.count("Which one?") == 1
    assert again.response_text.endswith("I already asked which saved contact you mean, so I won't pick one of their addresses myself.")
