"""Windows: a send whose approval card is waiting says so, never "I wasn't able to turn that into a completed action".

Steve, row 4 (0.7.3 at 8987cac7, 23 Nov 2026): "send 7hfBug4qLQyoyS2Nx2T3D4qwBaRRcKnwgPESE6SoCxMe 1 USDT". The model
called wallet.propose, then wallet.simulate on the new proposal. The simulation is a preview (simulate_only), the loop
stopped there with no text for the person, and the reply was the generic failure line while the 1 USDT card waited.
The card was approved and the transfer went through on-chain.

On Windows (core/wallet/transfers.py pending_approval_reply, core/agent_runtime/hive_topic_facade.py
_pending_payment_reply) a wallet preview whose proposal is pending approval answers with what it pays, to whom, where,
and that approving the card sends it. Off Windows the reply stays as Mac ships it.

SIMULATED CHAIN: the loopback Solana node answering as Mainnet (no real funds). Custody, proposals and the chat are real.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.wallet import transfers
from tests.test_windows_073_item6_usdt_chat_routing import _pending
from tests.test_windows_073_usdt_explicit_address import STEVE_ROW_4, solana_chat, solana_wallet
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine

pytestmark = [pytest.mark.safety]

GENERIC_FAILURE = "I wasn't able to turn that into a completed action."


def _propose_then_simulate(model: Any, monkeypatch: pytest.MonkeyPatch, propose_args: dict[str, Any]) -> list[str]:
    """The tiny lane's two rounds on Steve: wallet.propose, then wallet.simulate on the proposal it made."""
    from core.memory_first_router import ModelExecutionDecision

    router = model.session.agent.memory_router
    called: list[str] = []

    def decide(**_kwargs: Any) -> ModelExecutionDecision:
        if not called:
            output = {"intent": "wallet.propose", "arguments": dict(propose_args)}
        elif len(called) == 1:
            output = {"intent": "wallet.simulate", "arguments": {"proposal_id": _pending()[0].proposal_id}}
        else:
            output = {"intent": "respond.direct", "arguments": {"message": "Done."}}
        called.append(str(output["intent"]))
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"t{len(called)}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=output,
            confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )

    monkeypatch.setattr(router, "resolve_tool_intent", decide)
    return called


def _say(model: Any, text: str) -> str:
    return model.session.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


# --- served: Steve's row 4 --------------------------------------------------------------------------------------------

def test_on_windows_a_usdt_send_waiting_on_its_card_says_to_approve_the_card(solana_chat, monkeypatch) -> None:
    model, _node = solana_chat
    called = _propose_then_simulate(model, monkeypatch, {"destination": STEVE_ROW_4, "asset": "USDT", "amount": "1"})
    reply = _say(model, f"send {STEVE_ROW_4} 1 USDT")
    assert called[:2] == ["wallet.propose", "wallet.simulate"], called
    (proposal,) = _pending()
    assert (proposal.asset, int(proposal.amount_minor)) == ("USDT", 1_000_000)
    assert reply.startswith(
        "The payment is waiting for your approval: 1 USDT to 7hfBug4q…CxMe on Solana. Approve the card with your PIN, "
        "biometric or external wallet to send it. Nothing has been signed or sent yet."
    ), reply
    assert GENERIC_FAILURE not in reply and "Stopped safely" not in reply, reply


def test_on_windows_a_sol_send_waiting_on_its_card_takes_the_same_path(solana_chat, monkeypatch) -> None:
    model, _node = solana_chat
    _propose_then_simulate(model, monkeypatch, {"destination": STEVE_ROW_4, "asset": "SOL", "amount": "0.001"})
    reply = _say(model, f"send {STEVE_ROW_4} 0.001 SOL")
    (proposal,) = _pending()
    assert (proposal.asset, int(proposal.amount_minor)) == ("SOL", 1_000_000)
    assert reply.startswith("The payment is waiting for your approval: 0.001 SOL to 7hfBug4q…CxMe on Solana."), reply
    assert "Approve the card with your PIN" in reply and GENERIC_FAILURE not in reply, reply


def test_off_windows_the_reply_is_as_mac_ships_it(solana_chat, monkeypatch) -> None:
    model, _node = solana_chat
    _propose_then_simulate(model, monkeypatch, {"destination": STEVE_ROW_4, "asset": "SOL", "amount": "0.001"})
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    reply = _say(model, f"send {STEVE_ROW_4} 0.001 SOL")
    assert len(_pending()) == 1, reply
    assert "The payment is waiting for your approval" not in reply, reply
    assert GENERIC_FAILURE in reply, reply


# --- units: the sentence and when it applies ---------------------------------------------------------------------------

def _proposal(**fields: Any) -> SimpleNamespace:
    base = {"proposal_id": "p1", "network": "eip155:1", "asset": "USDT", "amount_minor": 2_500_000,
            "destination": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "state": "pending_approval"}
    return SimpleNamespace(**{**base, **fields})


@pytest.fixture
def proposals_stub(monkeypatch):
    from core.wallet import proposals

    held: dict[str, Any] = {}
    monkeypatch.setattr(proposals, "get_proposal", lambda pid: held.get(pid))
    return held


def test_on_windows_an_evm_usdt_proposal_gets_the_sentence_in_its_own_units(monkeypatch, proposals_stub) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    proposals_stub["p1"] = _proposal()
    assert transfers.pending_approval_reply("p1") == (
        "The payment is waiting for your approval: 2.5 USDT to 0x5aAeb6…eAed on Ethereum. Approve the card with your PIN, "
        "biometric or external wallet to send it. Nothing has been signed or sent yet."
    )


@pytest.mark.parametrize("state", ["confirmed", "rejected", "failed", "expired", "approved"])
def test_on_windows_a_proposal_no_longer_waiting_gets_no_sentence(monkeypatch, proposals_stub, state) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    proposals_stub["p1"] = _proposal(state=state)
    assert transfers.pending_approval_reply("p1") == ""


def test_on_windows_an_unknown_proposal_gets_no_sentence(monkeypatch, proposals_stub) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert transfers.pending_approval_reply("missing") == ""


def test_off_windows_no_proposal_gets_the_sentence(monkeypatch, proposals_stub) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    proposals_stub["p1"] = _proposal()
    assert transfers.pending_approval_reply("p1") == ""


def _execution(**fields: Any) -> SimpleNamespace:
    base = {"mode": "tool_preview", "status": "simulate_only", "user_safe_response_text": "",
            "details": {"observation": {"tool_surface": "wallet", "proposal_id": "p1"}}}
    return SimpleNamespace(**{**base, **fields})


@pytest.mark.parametrize("execution", [
    _execution(details={"observation": {"tool_surface": "wallet", "proposal_id": "p1"}}),  # wallet.simulate
    _execution(status="pending_approval", details={"proposal": {"proposal_id": "p1"}, "observation": {"tool_surface": "wallet"}}),
])
def test_on_windows_a_wallet_preview_of_a_waiting_proposal_is_answered_with_the_sentence(monkeypatch, proposals_stub, execution) -> None:
    from core.agent_runtime.hive_topic_facade import _pending_payment_reply

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    proposals_stub["p1"] = _proposal()
    assert _pending_payment_reply(execution).startswith("The payment is waiting for your approval: 2.5 USDT")


@pytest.mark.parametrize("execution", [
    _execution(mode="tool_failed"),
    _execution(details={"observation": {"tool_surface": "email", "proposal_id": "p1"}}),
    _execution(details={"observation": {"tool_surface": "wallet"}}),
])
def test_on_windows_anything_but_a_wallet_preview_with_a_proposal_keeps_the_old_line(monkeypatch, proposals_stub, execution) -> None:
    from core.agent_runtime.hive_topic_facade import _pending_payment_reply

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    proposals_stub["p1"] = _proposal()
    assert _pending_payment_reply(execution) == ""


# --- Steve row 38: the panic freeze is named as the reason, not a spending limit ---------------------------------------

def test_on_windows_a_frozen_wallet_refusal_says_payments_are_frozen(monkeypatch) -> None:
    from core.wallet import security

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    fault = security.wallet_fault("wallet_limit_exceeded", authority="core.wallet.lifecycle",
                                  context={"limit": "frozen", "reason": "spending is frozen (panic freeze is on)"})
    assert fault.user_message == "Payments are frozen (panic freeze is on), so it was not sent. Type /unfreeze to lift it."
    assert "spending limit" not in fault.user_message


@pytest.mark.parametrize("limit", ["per_transaction", "daily", "per_destination", ""])
def test_on_windows_a_real_limit_keeps_the_spending_limit_sentence(monkeypatch, limit) -> None:
    from core.wallet import security

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    fault = security.wallet_fault("wallet_limit_exceeded", authority="core.wallet.limits", context={"limit": limit})
    assert fault.user_message == "That payment is above a spending limit, so it was not sent."


def test_off_windows_a_frozen_wallet_refusal_is_as_mac_ships_it(monkeypatch) -> None:
    from core.wallet import security

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    fault = security.wallet_fault("wallet_limit_exceeded", authority="core.wallet.lifecycle", context={"limit": "frozen"})
    assert fault.user_message == "That payment is above a spending limit, so it was not sent."


def test_on_windows_a_send_with_the_freeze_on_is_told_the_freeze_is_why(solana_wallet, monkeypatch) -> None:
    from core.runtime_execution_tools import _dispatch_runtime_tool
    from core.wallet import limits

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    limits.set_frozen(True)
    try:
        result = _dispatch_runtime_tool("wallet.propose", {"destination": STEVE_ROW_4, "asset": "USDT", "amount": "1"},
                                        source_context={"session_id": "row38"})
    finally:
        limits.set_frozen(False)
    assert not result.ok and result.status == "wallet_limit_exceeded", (result.status, result.response_text)
    assert result.response_text.startswith("Payments are frozen (panic freeze is on), so it was not sent. Type /unfreeze to lift it.")
    assert "spending limit" not in result.response_text
    assert _pending() == []


# --- Steve row 10 (b): "drop" and a full Windows path gets the Drop answer, not a promise ------------------------------

@pytest.mark.parametrize("text", [
    "drop C:\\Users\\Satoshi\\Documents\\VOOL Windows\\missions\\capability-test\\r073-final2\\marked\\drop-test.txt",
    'drop "C:\\x\\y.txt"',
    "drop the file D:\\a.txt",
    "drop \\\\server\\share\\a.txt",
])
def test_on_windows_drop_and_a_full_path_gets_the_drop_answer(monkeypatch, text) -> None:
    from core import posix_tools
    from core.agent_runtime import fast_paths_windows_unavailable as lane

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", True)
    assert lane.windows_unavailable_answer(text) == (lane.WINDOWS_DROP_ANSWER, "drop_unavailable_on_windows")


@pytest.mark.parametrize("text", [
    "drop the last paragraph", "drop it", "drop notes.txt", "please drop C:\\x\\y.txt into the folder", "drop src\\a.py",
])
def test_ordinary_uses_of_drop_are_not_claimed(text) -> None:
    from core.agent_runtime import fast_paths_windows_unavailable as lane

    assert lane.windows_unavailable_answer(text) is None


def test_off_windows_drop_and_a_full_path_is_left_alone(monkeypatch) -> None:
    from core import posix_tools
    from core.agent_runtime import fast_paths_windows_unavailable as lane

    monkeypatch.setattr(posix_tools, "_IS_WINDOWS", False)
    result = lane.maybe_refuse_windows_unavailable(
        SimpleNamespace(_fast_path_result=lambda **kw: kw), "drop C:\\x\\y.txt", session_id="s", source_context={})
    assert result is None
