"""Windows: a question about a payment named with "to" is not a wallet send.

0.7.3 carry 6 (tests/test_windows_073_item6_usdt_chat_routing.py) taught the Windows wallet.propose
rule that a question about a payee named with no "to" ("did you send Bob 1 SOL?") asks about a
payment, not for one. The older "to" arm still read "did you send 1 SOL to Bob?" as a send and
offered wallet.propose. On Windows that arm now reads no question about a past or possible payment.
Every request still reaches the approval card: one put as a question ("can you send 1 SOL to Bob?"),
one opening with a clause ("When you can, send 1 SOL to Bob"), any imperative. Off Windows the rule is
the one Mac ships.
"""
from __future__ import annotations

import pytest

from core import windows_write_gate as gate
from core.tool_demand_signals import resolve_demand_signals

QUESTIONS = [
    "did you send 1 SOL to Bob?",
    "Did you send 1 SOL to Bob?",
    "have you sent 1 SOL to Bob yet?",
    "can you tell me if I sent 1 SOL to Bob",
    "did you send 1 SOL to Bob",
    "have I paid 2 USDC to Testy already",
    "has Tomas sent 5 USDC to Testy",
    "was it 1 USDT I sent to Testy",
    "were we going to send 1 SOL to Bob",
    "how do I send 1 USDT to Testy",
    "why did you send 0.5 SOL to Bob?",
    "when will you send 1 USDT to Testy",
]
REQUESTS = [
    "send 1 SOL to Bob",
    "When you can, send 1 SOL to Bob",
    "when you get a moment please send 1 SOL to Bob",
    "Have a look at the balance and send 1 SOL to Bob",
    "how about you send 1 SOL to Bob",
    "Have you got time to send 1 SOL to Bob?",
    "was going to ask: please send 1 SOL to Bob",
    "please send 1 SOL to Bob",
    "can you send 1 SOL to Bob?",
    "pay 0.5 USDT to Testy",
    "ok. transfer 2 USDC to Tomas Berg now",
]


def _is_send(text: str) -> bool:
    return "wallet.propose" in resolve_demand_signals(text).explicit_intents


@pytest.mark.parametrize("text", QUESTIONS)
def test_on_windows_a_question_about_a_to_payment_is_not_a_wallet_send(text, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert not _is_send(text)


@pytest.mark.parametrize("text", REQUESTS)
def test_on_windows_a_to_payment_request_is_still_a_wallet_send(text, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert _is_send(text)


def test_off_windows_the_to_arm_is_as_mac_ships_it(monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert _is_send("did you send 1 SOL to Bob?")
    assert _is_send("send 1 SOL to Bob")
