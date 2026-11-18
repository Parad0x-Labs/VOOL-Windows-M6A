"""0.7.3 carry 6: "send Testy 1 USDT" in chat reaches the wallet's USDT path.

Steve, 14 Nov (session openclaw:de83184b2bbc1085fb0f): "send Testy 1 USDT" seated no tool, because the
wallet.propose demand rule (core/tool_demand_signals.py) named lamports|sol|eth|bnb|usdc only and read a
saved name with no "to" as nothing. With no tool on the table the model copied an earlier card's
sentence, "Manual mode requires approval for this exact action...", although no approval was pending.

Now USDT is a wallet coin for the demand rule, and "send <saved name> <amount> <coin>" is a payment
shape. The seated wallet.propose resolves the contact and proposes through
``core.wallet.proposals.propose_transaction``, the same proposal the Crypto page's Send form makes
(/api/wallet/propose), and the review sheet's quote is in USDT's own units. The runtime's approval
sentence is kept out of a reply that no pending approval stands behind. All hosts.

Served as the VOOL window sends it: /api/chat through the real app (fixtures from
tests/test_windows_run_in_folder_served_end_to_end.py). The model is a stand-in that reads the
turn's REAL tool offer (the router's own ``_build_round_request``) and calls wallet.propose only when
the runtime seated it; otherwise it answers with the copied card sentence, as Steve's model did.
SIMULATED CHAIN: the loopback EVM node answering as Ethereum Mainnet (no real funds); the wallet's
own custody, Contacts and proposals are real.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.contacts import store
from tests.contacts import protected_changes
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine  # fixtures
from tests.wallet._rig_evm_native import ScriptedEvmNativeChain

pytestmark = [pytest.mark.safety]

ETHEREUM = "eip155:1"
USDT_ETHEREUM = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TESTY = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
SOL_TO = "2fgqQMyTitjLceky7ZTFwQDKCFjitKAc5A4EpVpk54FQ"
PIN = "482913"
COPIED_CARD_SENTENCE = "Manual mode requires approval for this exact action."


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch):
    """The USDT rule and the approval-text strip are Windows-only (`windows_write_gate`)."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


@pytest.fixture
def wallet_chat(chat, machine, monkeypatch):
    """A served chat whose home holds a ready Crypto Pilot account on Ethereum Mainnet (ETH for gas and
    50 USDT), and the saved contact Testy with one EVM wallet on Ethereum."""
    monkeypatch.setenv("VOOL_WALLET_ENABLED", "1")
    for name in ("VOOL_WALLET_NETWORK_ENVIRONMENT", "VOOL_WALLET_RPC_URLS", "VOOL_WALLET_TESTNET_RPC_URL", "VOOL_WALLET_GLOBAL_DAILY_MINOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOOL_WALLET_X402_ALLOW_LOOPBACK", "1")  # the scripted node listens on loopback
    session = chat(machine["desktop"])
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(machine["home"].parent / "blackbox"))
    from core.blackbox import store as store_module
    from core.wallet import chains, environment, pilot_custody

    protected_changes.reset()
    store_module.reset_default_store()
    chains.invalidate_chain_identity()
    environment.set_active_environment("mainnet")
    with ScriptedEvmNativeChain(chain_id=1, estimate_gas=60_000) as eth:
        monkeypatch.setenv("VOOL_WALLET_RPC_URLS", json.dumps({ETHEREUM: eth.url}))
        eth.add_token(USDT_ETHEREUM, decimals=6)
        created = pilot_custody.create_pilot_wallet(network=ETHEREUM, method="pin", credential=PIN, credential_confirmation=PIN,
                                                    creation_key=f"item6-{uuid.uuid4().hex}")
        revealed = pilot_custody.reveal_pilot_backup(created["wallet_id"], credential=PIN)
        wallet = pilot_custody.acknowledge_pilot_backup(created["wallet_id"], ack_token=revealed["ack_token"])
        eth.fund(wallet["address"], 10**17)
        eth.fund_token(USDT_ETHEREUM, wallet["address"], 50_000_000)
        store.create_contact(display_name="Testy", actor=store.ACTOR_OWNER, endpoints=[{"kind": "wallet", "value": TESTY, "network": ETHEREUM}])
        yield _OfferReadingModel(session, monkeypatch), eth
    chains.invalidate_chain_identity()
    store_module.reset_default_store()
    protected_changes.reset()


class _OfferReadingModel:
    """The chat's model: reads the round's real offer and calls wallet.propose with what the ask names
    only when the runtime seated it; with no wallet tool it copies the old card sentence."""

    def __init__(self, session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.memory_first_router import ModelExecutionDecision

        self.session = session
        self.offers: list[list[str]] = []
        self.call: dict[str, Any] = {}
        router = session.agent.memory_router

        def decide(**kwargs: Any) -> ModelExecutionDecision:
            request = router._build_round_request(
                task=kwargs["task"], classification=kwargs["classification"], interpretation=kwargs["interpretation"],
                context_result=kwargs["context_result"], persona=kwargs["persona"], output_mode="tool_intent",
                task_kind="tool_intent", surface=kwargs.get("surface") or "cli", source_context=kwargs.get("source_context"),
            )
            offered = sorted(str(tool.intent) for tool in (request.tools or []))
            self.offers.append(offered)
            seated = "wallet.propose" in offered and self.call and len(self.offers) == 1
            output = ({"intent": "wallet.propose", "arguments": dict(self.call)} if seated
                      else {"intent": "respond.direct", "arguments": {"message": COPIED_CARD_SENTENCE if len(self.offers) == 1 else "Done."}})
            return ModelExecutionDecision(
                source="provider_execution", task_hash=f"t{len(self.offers)}", provider_id="ollama-local:test",
                provider_name="ollama-local", model_name="test", structured_output=output,
                confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
            )

        def answer(*_args: Any, **_kwargs: Any) -> ModelExecutionDecision:
            # a turn with no tool lane: the model's plain answer, the copied card sentence
            self.answers += 1
            return ModelExecutionDecision(
                source="model", task_hash=f"a{self.answers}", provider_id="ollama-local:test", provider_name="ollama-local",
                model_name="test", output_text=COPIED_CARD_SENTENCE, confidence=0.9, trust_score=0.9, used_model=True,
            )

        self.answers = 0
        monkeypatch.setattr(router, "resolve_tool_intent", decide)
        monkeypatch.setattr(router, "resolve", answer)

    def ask(self, text: str, call: dict[str, Any]) -> str:
        self.offers, self.call = [], dict(call)
        return self.session.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


def _pending() -> list[Any]:
    from core.wallet import proposals

    return [p for p in proposals.list_proposals() if p.state == "pending_approval"]


def test_send_testy_1_usdt_seats_the_wallet_and_proposes_1_usdt_in_its_own_units(wallet_chat) -> None:
    from core.wallet import quotes

    model, eth = wallet_chat
    reply = model.ask("send Testy 1 USDT", {"destination": "Testy", "asset": "USDT", "amount": "1"})
    assert model.offers and "wallet.propose" in model.offers[0], model.offers
    pending = _pending()
    assert len(pending) == 1, reply
    proposal = pending[0]
    assert (proposal.asset, int(proposal.amount_minor), proposal.network, proposal.destination.lower()) == ("USDT", 1_000_000, ETHEREUM, TESTY.lower()), proposal
    assert "1000000 USDT" in reply and "Testy" in reply and COPIED_CARD_SENTENCE not in reply, reply

    # the review sheet the owner approves on: the same quote the Crypto page's Send form opens
    fields = quotes.mint_quote(proposal.proposal_id)["fields"]
    assert (fields["asset"], fields["amount_human"], fields["gas_asset"], fields["token_contract"]) == ("USDT", "1", "ETH", USDT_ETHEREUM), fields
    assert fields["recipient_saved"] is True and fields["recipient_label"] == "Testy", fields
    assert eth.send_count() == 0, "a proposal sends nothing"


def test_send_testy_eth_in_the_same_words_proposes_the_native_coin(wallet_chat) -> None:
    model, eth = wallet_chat
    reply = model.ask("send Testy 0.001 ETH", {"destination": "Testy", "asset": "ETH", "amount": "0.001"})
    assert "wallet.propose" in model.offers[0], model.offers
    (proposal,) = _pending()
    assert (proposal.asset, int(proposal.amount_minor), proposal.network) == ("ETH", 10**15, ETHEREUM), reply
    assert eth.send_count() == 0


@pytest.mark.parametrize("text", [
    f"Send 0.0005 SOL on solana to {SOL_TO} for the devnet proof",
    "pay 0x5555555555555555555555555555555555555555 1 USDC on base",
    "send 1 USDC to Testy",
    "send 0.5 SOL to Testy",
])
def test_sol_and_usdc_asks_still_seat_the_wallet(wallet_chat, text: str) -> None:
    from core.tool_demand_signals import resolve_demand_signals

    model, _eth = wallet_chat
    signals = resolve_demand_signals(text)
    assert signals.explicit_intents == ("wallet.propose",) and signals.required_families == ("wallet",)
    model.ask(text, {})
    assert "wallet.propose" in model.offers[0], model.offers


@pytest.mark.parametrize("text", [
    "send me 1 SOL",
    "pay for 1 ETH",
    "what is the price of USDT",
    "send me the report on solar power to review",
    "transfer the 2.5 GB archive to the other laptop",
])
def test_words_that_name_no_payee_are_still_not_a_wallet_demand(text: str) -> None:
    from core.tool_demand_signals import resolve_demand_signals

    assert "wallet.propose" not in resolve_demand_signals(text).explicit_intents


@pytest.mark.parametrize("text", [
    "did you send Bob 1 SOL",
    "have I paid Testy 1 USDT already",
    "has Tomas sent Testy 5 USDC",
    "was it pay Testy 1 USDT or 2",
    "were we going to send Bob 1 SOL",
    "how do I send Testy 1 USDT",
    "why did you send Bob 1 SOL",
    "when will you send Testy 1 USDT",
    "did you send Bob 1 SOL?",
    "send Testy 1 USDT?",
])
def test_a_question_about_a_named_payee_payment_is_not_a_wallet_demand(text: str) -> None:
    """The no-"to" payee arm reads a request only: a question asks about a payment, it does not ask for one."""
    from core.tool_demand_signals import resolve_demand_signals

    assert "wallet.propose" not in resolve_demand_signals(text).explicit_intents


def test_the_same_payee_request_with_a_lead_in_is_still_a_wallet_demand() -> None:
    from core.tool_demand_signals import resolve_demand_signals

    for text in ("please send Testy 1 USDT", "ok. send Testy 1 USDT now", "send Testy 1 USDT. Thanks!"):
        assert resolve_demand_signals(text).explicit_intents == ("wallet.propose",), text


def test_a_copied_approval_sentence_with_nothing_pending_is_not_shown(wallet_chat) -> None:
    model, _eth = wallet_chat
    reply = model.ask("send Testy 1 DOGE", {})  # no wallet coin: nothing seated, the model copies the card
    assert COPIED_CARD_SENTENCE not in reply and "requires approval for this exact action" not in reply, reply
    assert model.answers + len(model.offers) >= 1, "the model answered this turn"
    assert reply == "Nothing from this turn is waiting for your approval.", reply
    assert not model.session.pending()


def test_the_runtime_card_sentence_stays_while_its_approval_is_pending() -> None:
    from core import mode_permission_policy as mpp
    from core.agent_runtime.action_honesty_validator import _strip_runtime_approval_text_without_a_card

    card = ("`workspace.write_file` needs your approval before it runs. Manual mode requires approval for this exact action. "
            "Review the details, then allow or deny this request.")
    mpp.reset_mode_permission_state()
    session = f"openclaw:{uuid.uuid4().hex[:20]}"
    assert _strip_runtime_approval_text_without_a_card({"response": card, "approval_request": {"approval_id": "a1"}}, session_id=session)["response"] == card
    mpp._APPROVALS["a1"] = {"session_id": session, "status": "pending", "expires_at": 4102444800.0}
    try:
        assert _strip_runtime_approval_text_without_a_card({"response": card}, session_id=session)["response"] == card
        stripped = _strip_runtime_approval_text_without_a_card({"response": "Sure. " + card + "\nAnything else?"}, session_id="openclaw:other")
        assert stripped["response"] == "Sure.\nAnything else?", stripped
        assert stripped["action_honesty_validator"]["reason"] == "runtime_approval_text_without_pending_approval"
    finally:
        mpp.reset_mode_permission_state()


def test_off_windows_the_wallet_rule_and_the_reply_are_as_mac_ships_them(monkeypatch) -> None:
    from core import tool_demand_signals as tds
    from core.agent_runtime.action_honesty_validator import _strip_runtime_approval_text_without_a_card

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert tds._rules() is tds._RULES
    assert "usdt" not in next(p.pattern for i, _f, p in tds._RULES if i == "wallet.propose").lower()
    for text in ("send Testy 1 USDT", "send 1 USDT to Testy", "send Testy 1 SOL"):
        assert "wallet.propose" not in tds.resolve_demand_signals(text).explicit_intents, text
    assert tds.resolve_demand_signals("send 0.5 SOL to Testy").explicit_intents == ("wallet.propose",)
    copied = {"response": COPIED_CARD_SENTENCE}
    assert _strip_runtime_approval_text_without_a_card(dict(copied), session_id="openclaw:none") == copied
