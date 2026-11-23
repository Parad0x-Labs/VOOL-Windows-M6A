"""Windows: "send <Solana address> 1 USDT" reaches a 1 USDT approval card, whatever chain and network the model guessed.

Steve, row 4 (23 Nov): "send 7hfBug4qLQyoyS2Nx2T3D4qwBaRRcKnwgPESE6SoCxMe 1 USDT" failed. The tiny lane's model called
wallet.propose with chain=ethereum and network=mainnet. "mainnet" is an environment, not a network, so the recipient
resolver refused it ('mainnet' is not a network this wallet declares) before it looked at the address, and the chain
hints also mapped "mainnet" to Ethereum. The turn told the owner the request was invalid.

On Windows (core/wallet/transfers.py explicit_address_hints, chain_hint):
* "mainnet", "testnet" and "devnet" in the network or the chain count as unset;
* an address given directly says its own family: base58 is Solana, 0x is EVM. A chain of the other family that came with
  an environment word for the network was guessed from that word and is dropped; one named on its own is refused;
* the network left unset is the active environment's row of that family that carries the asset.
Off Windows the values reach the wallet as given, as Mac ships it.

SIMULATED CHAIN: the loopback Solana node answering as Mainnet (no real funds). Custody, proposals and the chat are real.
"""
from __future__ import annotations

import json
import uuid

import pytest

from core import windows_write_gate as gate
from core.wallet import transfers
from tests.contacts import protected_changes
from tests.test_windows_073_item6_usdt_chat_routing import _OfferReadingModel, _pending
from tests.test_windows_run_in_folder_served_end_to_end import chat, machine  # fixtures
from tests.wallet._simulated_solana import MAINNET_GENESIS, SimulatedSolanaNode

pytestmark = [pytest.mark.safety]

SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STEVE_ROW_4 = "7hfBug4qLQyoyS2Nx2T3D4qwBaRRcKnwgPESE6SoCxMe"
EVM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
PIN = "482913"


def _setup_solana(monkeypatch, node) -> dict:
    from core.wallet import chains, environment, pilot_custody

    monkeypatch.setenv("VOOL_WALLET_RPC_URLS", json.dumps({SOLANA_MAINNET: node.url}))
    chains.invalidate_chain_identity()
    environment.set_active_environment("mainnet")
    node.create_mint(USDT_MINT, decimals=6)
    created = pilot_custody.create_pilot_wallet(network=SOLANA_MAINNET, method="pin", credential=PIN, credential_confirmation=PIN,
                                                creation_key=f"row4-{uuid.uuid4().hex}", label="Solana")
    revealed = pilot_custody.reveal_pilot_backup(created["wallet_id"], credential=PIN)
    wallet = pilot_custody.acknowledge_pilot_backup(created["wallet_id"], ack_token=revealed["ack_token"])
    node.fund_sol(wallet["address"], 20_000_000)
    node.fund_token(wallet["address"], USDT_MINT, 25_000_000)
    node.fund_sol(STEVE_ROW_4, 5_000_000)
    node.open_token_account(STEVE_ROW_4, USDT_MINT)
    return wallet


def _wallet_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VOOL_WALLET_ENABLED", "1")
    monkeypatch.setenv("VOOL_WALLET_X402_ALLOW_LOOPBACK", "1")  # the simulated node listens on loopback
    for name in ("VOOL_WALLET_NETWORK_ENVIRONMENT", "VOOL_WALLET_TESTNET_RPC_URL", "VOOL_WALLET_GLOBAL_DAILY_MINOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox"))
    from core.blackbox import store as store_module

    protected_changes.reset()
    store_module.reset_default_store()


def _teardown() -> None:
    from core.blackbox import store as store_module
    from core.wallet import chains

    chains.invalidate_chain_identity()
    store_module.reset_default_store()
    protected_changes.reset()


@pytest.fixture
def solana_chat(chat, machine, monkeypatch):
    """A served chat whose home holds a ready Crypto Pilot account on Solana Mainnet with SOL and 25 USDT."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _wallet_env(monkeypatch, machine["home"].parent)
    session = chat(machine["desktop"])
    with SimulatedSolanaNode(genesis_hash=MAINNET_GENESIS) as node:
        _setup_solana(monkeypatch, node)
        yield _OfferReadingModel(session, monkeypatch), node
    _teardown()


@pytest.fixture
def solana_wallet(monkeypatch, tmp_path):
    """The same account with no chat: wallet.propose called directly, as the tool lane calls it."""
    _wallet_env(monkeypatch, tmp_path)
    with SimulatedSolanaNode(genesis_hash=MAINNET_GENESIS) as node:
        _setup_solana(monkeypatch, node)
        yield node
    _teardown()


def _propose(**args):
    from core.runtime_execution_tools import _dispatch_runtime_tool

    return _dispatch_runtime_tool("wallet.propose", dict(args), source_context={"session_id": "row4"})


# --- served: Steve's row 4 ----------------------------------------------------------------------------------------------

def test_on_windows_send_a_solana_address_1_usdt_reaches_a_1_usdt_approval_card(solana_chat) -> None:
    model, _node = solana_chat
    reply = model.ask(f"send {STEVE_ROW_4} 1 USDT",
                      {"destination": STEVE_ROW_4, "asset": "USDT", "amount": "1", "chain": "ethereum", "network": "mainnet"})
    assert model.offers and "wallet.propose" in model.offers[0], model.offers
    pending = _pending()
    assert len(pending) == 1, reply
    proposal = pending[0]
    assert (proposal.asset, int(proposal.amount_minor), proposal.network, proposal.destination) == (
        "USDT", 1_000_000, SOLANA_MAINNET, STEVE_ROW_4), proposal
    lowered = reply.lower()
    assert "invalid" not in lowered and "not a network" not in lowered and "not an address" not in lowered, reply


# --- the tool lane, called directly -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("chain", "network"), [("ethereum", "mainnet"), ("mainnet", "mainnet"), ("", "mainnet"), ("mainnet", ""), ("solana", "mainnet")])
def test_on_windows_environment_words_and_a_guessed_chain_still_propose_on_solana(solana_wallet, monkeypatch, chain, network) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    result = _propose(destination=STEVE_ROW_4, asset="USDT", amount="1", chain=chain, network=network)
    assert result.ok, result.response_text
    (proposal,) = _pending()
    assert (proposal.asset, int(proposal.amount_minor), proposal.network) == ("USDT", 1_000_000, SOLANA_MAINNET)


def test_on_windows_a_chain_named_on_its_own_that_contradicts_the_address_is_refused(solana_wallet, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    result = _propose(destination=STEVE_ROW_4, asset="USDT", amount="1", chain="ethereum")
    assert (result.ok, result.status) == (False, "recipient_chain_mismatch"), result.response_text
    text = result.response_text
    assert "Solana address" in text and "Ethereum" in text and "Nothing was proposed" in text, text
    assert "invalid" not in text.lower(), "a valid address is never called invalid"
    assert _pending() == []


def test_off_windows_the_tool_lane_is_as_mac_ships_it(solana_wallet, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    result = _propose(destination=STEVE_ROW_4, asset="USDT", amount="1", chain="ethereum", network="mainnet")
    assert not result.ok and "'mainnet' is not a network this wallet declares" in result.response_text, result.response_text
    assert _pending() == []


# --- unit rows ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("word", "windows", "expected"), [
    ("mainnet", True, ""), ("Testnet", True, ""), ("devnet", True, ""), ("eth", True, "ethereum"), ("sol", True, "solana"),
    ("mainnet", False, "ethereum"), ("eth", False, "ethereum"), ("testnet", False, "testnet"),
])
def test_chain_hint(monkeypatch, word, windows, expected) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    assert transfers.chain_hint(word) == expected


@pytest.mark.parametrize(("destination", "chain", "network", "expected"), [
    (STEVE_ROW_4, "ethereum", "mainnet", ("", "")),
    (STEVE_ROW_4, "solana", "mainnet", ("solana", "")),
    (STEVE_ROW_4, "mainnet", "devnet", ("", "")),
    (EVM, "solana", "testnet", ("", "")),
    (EVM, "base", "mainnet", ("base", "")),
    (EVM, "ethereum", "eip155:1", ("ethereum", "eip155:1")),
    ("Testy", "ethereum", "mainnet", ("ethereum", "")),
])
def test_on_windows_explicit_address_hints_fit_the_address(monkeypatch, destination, chain, network, expected) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    fitted = transfers.explicit_address_hints(destination, chain=chain, network=network)
    assert ((fitted.chain, fitted.network), fitted.refusal) == (expected, "")


@pytest.mark.parametrize(("destination", "chain", "shape", "named"), [
    (STEVE_ROW_4, "ethereum", "a Solana address", "Ethereum"),
    (STEVE_ROW_4, "base", "a Solana address", "Base"),
    (EVM, "solana", "an EVM (0x) address", "Solana"),
])
def test_on_windows_a_contradicting_chain_is_refused_without_calling_the_address_invalid(monkeypatch, destination, chain, shape, named) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    fitted = transfers.explicit_address_hints(destination, chain=chain, network="")
    assert shape in fitted.refusal and named in fitted.refusal and "invalid" not in fitted.refusal.lower(), fitted.refusal


def test_off_windows_explicit_address_hints_pass_the_values_through(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    fitted = transfers.explicit_address_hints(STEVE_ROW_4, chain="ethereum", network="mainnet")
    assert ((fitted.chain, fitted.network), fitted.refusal) == (("ethereum", "mainnet"), "")
