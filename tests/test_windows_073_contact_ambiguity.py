"""Windows: when a contact name matches more than one saved contact, the model can't answer "which one?" itself.

Steve, row 38 (0.7.3 at 8987cac7, 23 Nov 2026, receipt pay-602f028e57c04593a351): "send Testy 1 USDT" with two saved
contacts matching "Testy". wallet.propose asked which one twice, then the model called it again with one contact's raw
address and chain=solana, which resolves as an address given directly, and a proposal was drafted for a recipient
nobody chose. On Windows (core/runtime_execution_tools.py _remember_recipient_question / _asked_recipient_question) the
recipients a "which one?" answer offered are held for that turn (their addresses, ct-/ep- ids and names, merged over
every question the turn asked), and a later call in the same turn naming one of them, or resolving to one of them by
any spelling the resolver takes (an alias, "Name(label)"), gets the same question. The person's own answer comes on a later turn, which is never blocked. Off Windows the
wallet answers as Mac ships it.

SIMULATED CHAIN: the loopback Solana node answering as Mainnet (no real funds). Custody, proposals and the chat are real.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from core import runtime_execution_tools as rt
from core import windows_write_gate as gate
from tests.test_windows_073_item6_usdt_chat_routing import _pending
from tests.test_windows_073_usdt_explicit_address import SOLANA_MAINNET, STEVE_ROW_4, USDT_MINT, solana_chat
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

pytestmark = [pytest.mark.safety]

OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
STRANGER = "So11111111111111111111111111111111111111112"
ZED_ONE = "Vote111111111111111111111111111111111111111"
ZED_TWO = "Stake11111111111111111111111111111111111111"
QUESTION = "2 saved contacts match 'Testy'"


@pytest.fixture(autouse=True)
def _no_held_questions():
    rt._RECIPIENT_QUESTIONS.clear()
    yield
    rt._RECIPIENT_QUESTIONS.clear()


@pytest.fixture
def two_testys(solana_chat):
    from core.contacts import store

    model, node = solana_chat
    for address in (OTHER, STRANGER):
        node.fund_sol(address, 5_000_000)
        node.open_token_account(address, USDT_MINT)
    store.create_contact(display_name="Testy Ann", actor=store.ACTOR_OWNER, aliases=["Annie"],
                         endpoints=[{"kind": "wallet", "value": STEVE_ROW_4, "network": SOLANA_MAINNET}])
    store.create_contact(display_name="Testy Bob", actor=store.ACTOR_OWNER,
                         endpoints=[{"kind": "wallet", "value": OTHER, "network": SOLANA_MAINNET}])
    return model


def _script(model: Any, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """The stand-in model makes `calls` (wallet.propose arguments) in order each turn, then answers; returns what each
    wallet call saw: (destination, turn key, session, status)."""
    from core.faults.recorder import identity_from_context
    from core.memory_first_router import ModelExecutionDecision

    seen: list[tuple[str, str, str, str]] = []
    real = rt._wallet_model_tool

    def spy(intent: str, arguments: dict[str, Any], source_context: dict[str, Any] | None):
        result = real(intent, arguments, source_context)
        turn, session = identity_from_context(source_context)
        seen.append((str(arguments.get("destination") or ""), turn, session, str(result.status)))
        return result

    monkeypatch.setattr(rt, "_wallet_model_tool", spy)
    rounds: list[int] = []

    def decide(**_kwargs: Any) -> ModelExecutionDecision:
        rounds.append(1)
        n = len(rounds)
        output = ({"intent": "wallet.propose", "arguments": dict(calls[n - 1])} if n <= len(calls)
                  else {"intent": "respond.direct", "arguments": {"message": "Done."}})
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"t{n}", provider_id="ollama-local:test", provider_name="ollama-local",
            model_name="test", structured_output=output, confidence=0.8, trust_score=0.84, used_model=True,
            validation_state="valid",
        )

    monkeypatch.setattr(model.session.agent.memory_router, "resolve_tool_intent", decide)
    model.rounds = rounds
    return seen


def _turn(model: Any, text: str) -> str:
    model.rounds.clear()
    return model.session.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


def _by_name(name: str = "Testy") -> dict[str, Any]:
    return {"destination": name, "asset": "USDT", "amount": "1"}


def _by_address(address: str) -> dict[str, Any]:
    return {"destination": address, "asset": "USDT", "amount": "1", "chain": "solana"}


# --- served ------------------------------------------------------------------------------------------------------------

def test_the_turn_key_is_one_across_tool_rounds_and_new_for_each_turn(two_testys, monkeypatch) -> None:
    seen = _script(two_testys, monkeypatch, [_by_name(), _by_address(STRANGER)])
    _turn(two_testys, "send Testy 1 USDT")
    _turn(two_testys, "send Testy 1 USDT")
    turns = [turn for _d, turn, _s, _st in seen]
    sessions = {session for _d, _t, session, _st in seen}
    assert len(turns) == 4 and all(turns), seen
    assert turns[0] == turns[1] and turns[2] == turns[3] and turns[0] != turns[2], turns
    assert len(sessions) == 1 and all(sessions), sessions


def test_on_windows_the_model_cannot_answer_which_one_with_a_raw_address_in_the_same_turn(two_testys, monkeypatch) -> None:
    seen = _script(two_testys, monkeypatch, [_by_name(), _by_address(STEVE_ROW_4)])
    _turn(two_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous", "recipient_ambiguous"], seen
    assert _pending() == []


def test_on_windows_the_same_turn_refusal_repeats_the_question(two_testys, monkeypatch) -> None:
    from core.faults.recorder import identity_from_context

    _script(two_testys, monkeypatch, [_by_name()])
    texts: list[str] = []
    real = rt._wallet_model_tool

    def capture(intent: str, arguments: dict[str, Any], source_context: dict[str, Any] | None):
        result = real(intent, arguments, source_context)
        texts.append(result.response_text)
        if len(texts) == 1:  # the model's second call in the same turn, with Testy Bob's address
            again = real(intent, _by_address(OTHER), source_context)
            texts.append(again.response_text)
            assert identity_from_context(source_context)[0]
        return result

    monkeypatch.setattr(rt, "_wallet_model_tool", capture)
    _turn(two_testys, "send Testy 1 USDT")
    assert len(texts) == 2, texts
    assert texts[0].startswith(QUESTION) and texts[1].startswith(texts[0]), texts
    assert texts[1].endswith("I already asked which saved contact you mean, so I won't pick one of their addresses myself."), texts[1]
    assert _pending() == []


def test_on_windows_the_persons_pick_on_the_next_turn_proposes(two_testys, monkeypatch) -> None:
    seen = _script(two_testys, monkeypatch, [_by_name()])
    _turn(two_testys, "send Testy 1 USDT")
    seen_next = _script(two_testys, monkeypatch, [_by_address(STEVE_ROW_4)])
    _turn(two_testys, f"send {STEVE_ROW_4} 1 USDT")
    assert seen[0][3] == "recipient_ambiguous" and seen_next[0][3] == "pending_approval", (seen, seen_next)
    (proposal,) = _pending()
    assert proposal.destination == STEVE_ROW_4


def test_on_windows_an_address_that_was_not_offered_still_proposes_in_the_same_turn(two_testys, monkeypatch) -> None:
    seen = _script(two_testys, monkeypatch, [_by_name(), _by_address(STRANGER)])
    _turn(two_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous", "pending_approval"], seen


def test_on_windows_an_alias_of_an_offered_contact_is_held_after_it_resolves(two_testys, monkeypatch) -> None:
    # "Annie" is Testy Ann's alias: the typed string names no offered value, but it resolves to Testy Ann
    seen = _script(two_testys, monkeypatch, [_by_name(), _by_name("Annie")])
    _turn(two_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous", "recipient_ambiguous"], seen
    assert _pending() == []


def test_on_windows_the_alias_of_an_offered_contact_proposes_on_the_next_turn(two_testys, monkeypatch) -> None:
    _script(two_testys, monkeypatch, [_by_name()])
    _turn(two_testys, "send Testy 1 USDT")
    seen_next = _script(two_testys, monkeypatch, [_by_name("Annie")])
    _turn(two_testys, "send Annie 1 USDT")
    assert seen_next[0][3] == "pending_approval", seen_next
    (proposal,) = _pending()
    assert proposal.destination == STEVE_ROW_4


def test_on_windows_a_second_question_in_the_turn_does_not_free_the_first_ones_recipients(two_testys, monkeypatch) -> None:
    from core.contacts import store

    for name, address in (("Zed One", ZED_ONE), ("Zed Two", ZED_TWO)):
        store.create_contact(display_name=name, actor=store.ACTOR_OWNER,
                             endpoints=[{"kind": "wallet", "value": address, "network": SOLANA_MAINNET}])
    seen = _script(two_testys, monkeypatch, [_by_name(), _by_name("Zed"), _by_address(STEVE_ROW_4), _by_address(ZED_ONE)])
    _turn(two_testys, "send Testy 1 USDT")
    assert [status for _d, _t, _s, status in seen] == ["recipient_ambiguous"] * 4, seen
    assert _pending() == []


def test_off_windows_the_wallet_answers_as_mac_ships_it(two_testys, monkeypatch) -> None:
    # the same two calls in one turn, made the way the tool loop makes them, with the Windows gate off
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    context = {"turn_id": f"turn-{uuid.uuid4().hex[:10]}", "session_id": "row38-off-windows"}
    first = rt._wallet_model_tool("wallet.propose", _by_name(), context)
    second = rt._wallet_model_tool("wallet.propose", _by_address(STEVE_ROW_4), context)
    assert (first.status, second.status) == ("recipient_ambiguous", "pending_approval"), (first.response_text, second.response_text)


# --- units -------------------------------------------------------------------------------------------------------------

_RESOLUTION = {"choices": [
    {"choose_with": "ct-ann", "contact_id": "ct-ann", "display_name": "Testy Ann",
     "endpoints": [{"choose_with": "ep-ann", "endpoint_id": "ep-ann", "contact_id": "ct-ann", "display_name": "Testy Ann", "value": STEVE_ROW_4}]},
    {"choose_with": "ct-bob", "contact_id": "ct-bob", "display_name": "Testy Bob",
     "endpoints": [{"value": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"}]},
]}
_CONTEXT = {"turn_id": "turn-1", "session_id": "s1"}


def test_on_windows_an_evm_address_is_matched_whatever_its_case(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._asked_recipient_question("0x5AAEB6053f3e94c9b9a09f33669435e7ef1beaed", _CONTEXT) == "which one?"
    assert rt._asked_recipient_question(STEVE_ROW_4, _CONTEXT) == "which one?"


@pytest.mark.parametrize("named", ["ct-ann", "ep-ann", "ct-bob", "Testy Ann", "testy bob", "Testy Ann (main)", "Testy Ann(main)"])
def test_on_windows_an_offered_contact_by_id_or_name_is_held_too(monkeypatch, named) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._asked_recipient_question(named, _CONTEXT) == "which one?"


@pytest.mark.parametrize("named", ["Testy", "Testy Carl", "ct-carl", "ep-other", "Tess"])
def test_on_windows_a_name_or_id_that_was_not_offered_is_not_held(monkeypatch, named) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._asked_recipient_question(named, _CONTEXT) == ""


def test_on_windows_a_second_question_in_the_turn_adds_to_the_first_and_keeps_its_text(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which Testy?", _CONTEXT)
    rt._remember_recipient_question({"choices": [{"contact_id": "ct-zed", "display_name": "Zed One", "value": ZED_ONE},
                                                 {"contact_id": "ct-ann", "display_name": "Testy Ann"}]}, "which Zed?", _CONTEXT)
    assert rt._asked_recipient_question(STEVE_ROW_4, _CONTEXT) == "which Testy?"
    assert rt._asked_recipient_question("ct-ann", _CONTEXT) == "which Testy?"
    assert rt._asked_recipient_question(ZED_ONE, _CONTEXT) == "which Zed?"


@pytest.mark.parametrize(
    ("snapshot", "held"),
    (
        ({"resolution": "contact", "contact_id": "ct-ann", "endpoint_id": "ep-x", "value": "elsewhere"}, True),
        ({"resolution": "contact", "contact_id": "ct-x", "endpoint_id": "ep-ann", "value": "elsewhere"}, True),
        ({"resolution": "explicit", "value": STEVE_ROW_4}, True),
        ({"resolution": "explicit", "value": "0x5AAEB6053F3E94C9B9A09F33669435E7EF1BEAED"}, True),
        ({"resolution": "contact", "contact_id": "ct-carl", "endpoint_id": "ep-carl", "value": STRANGER}, False),
        ({"resolution": "explicit", "value": STRANGER}, False),
    ),
)
def test_on_windows_what_a_destination_resolved_to_is_checked_against_the_offered_recipients(monkeypatch, snapshot, held) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._asked_about_resolved_recipient(snapshot, _CONTEXT) == ("which one?" if held else "")
    assert rt._asked_about_resolved_recipient(snapshot, {"turn_id": "turn-2", "session_id": "s1"}) == ""


def test_on_windows_another_turn_or_session_is_never_blocked(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._asked_recipient_question(STEVE_ROW_4, {"turn_id": "turn-2", "session_id": "s1"}) == ""
    assert rt._asked_recipient_question(STEVE_ROW_4, {"turn_id": "turn-1", "session_id": "s2"}) == ""


def test_on_windows_a_turn_with_no_identity_holds_nothing(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", {"session_id": "s1"})
    assert rt._RECIPIENT_QUESTIONS == {}
    assert rt._asked_recipient_question(STEVE_ROW_4, {"session_id": "s1"}) == ""


def test_on_windows_a_held_question_expires(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    later = rt.time.monotonic() + rt._RECIPIENT_QUESTION_TTL_SECONDS + 1
    monkeypatch.setattr(rt.time, "monotonic", lambda: later)
    assert rt._asked_recipient_question(STEVE_ROW_4, _CONTEXT) == ""


def test_off_windows_nothing_is_held(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    rt._remember_recipient_question(_RESOLUTION, "which one?", _CONTEXT)
    assert rt._RECIPIENT_QUESTIONS == {}
    assert rt._asked_recipient_question(STEVE_ROW_4, _CONTEXT) == ""
    assert rt._asked_about_resolved_recipient({"resolution": "explicit", "value": STEVE_ROW_4}, _CONTEXT) == ""
