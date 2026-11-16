"""Windows: an approved market.collect runs; the approval is not asked for a second time.

In manual mode the model's market.collect call (argument `token`: the coin's mint or contract
address) raised an approval card. Once allowed and the turn resent, VOOL minted a second card instead
of running the call: the pending call is stored for the resume with every argument whose name holds
"token" masked as a secret, so the resumed call carried `token: "[redacted]"`, its approval
fingerprint no longer matched the allowed one, and nothing was collected. Found serving item 8 of
CARRY-FROM-0.7.2 (Steve r072-4). The bug is the same on every host.

Served end to end through /api/chat with the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py; the model is stubbed.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core import mode_permission_policy as mpp
from tests.asgi_harness import asgi_request
from tests.test_windows_run_in_folder_served_end_to_end import HEADERS, chat, machine

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
#: Reaches the tool loop on this base (a no-guessing ask is grounded) and seats market.collect.
ASK = f"collect the swaps for {MINT} over the last week. Do not guess."
CALL = {"intent": "market.collect", "arguments": {"token": MINT, "chain": "solana", "window": "7d"}}


def _stub_model(talk, monkeypatch) -> list[dict[str, Any]]:
    """The model asks for market.collect, and answers in prose once a tool result is in the turn."""
    from core.memory_first_router import ModelExecutionDecision

    rounds: list[dict[str, Any]] = []

    def decide(**kwargs: Any) -> ModelExecutionDecision:
        rounds.append(kwargs)
        seen = json.dumps(kwargs, default=str)
        out = (
            {"intent": "respond.direct", "arguments": {"message": "Collected."}}
            if "Collecting trades uses public DEX candles" in seen
            else CALL
        )
        return ModelExecutionDecision(
            source="provider_execution", task_hash="t-collect", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=out,
            confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )

    monkeypatch.setattr(talk.agent.memory_router, "resolve_tool_intent", decide)
    return rounds


def _post(talk, text: str, *, turn_id: str, token: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": text}], "session_id": talk.session, "stream": False,
        "mode": "manual", "turn_id": turn_id,
    }
    if token:
        body["approval_token"] = token
    status, _headers, raw = asgi_request(
        talk.app, method="POST", path="/api/chat", headers=HEADERS, body=json.dumps(body).encode("utf-8")
    )
    assert status == 200, raw[:400]
    return json.loads(raw.decode("utf-8"))


def test_served_an_approved_market_collect_runs(machine, chat, monkeypatch) -> None:
    talk = chat(machine["desktop"])
    _stub_model(talk, monkeypatch)
    ran: list[dict[str, Any]] = []
    from core import market_data

    real = market_data._collect

    def collect(args, *a, **k):
        ran.append(dict(args))
        return real(args, *a, **k)

    monkeypatch.setattr(market_data, "_collect", collect)
    turn = f"turn-{uuid.uuid4().hex[:10]}"

    _post(talk, ASK, turn_id=turn)
    pending = talk.pending()
    assert len(pending) == 1 and pending[0]["intent"] == "market.collect", pending
    assert ran == [], "market.collect ran before it was approved"
    card = str(pending[0]["approval_id"])
    assert mpp.resolve_approval(card, decision="allow") is not None

    resumed = _post(talk, ASK, turn_id=turn, token=card)
    reply = str(resumed["message"]["content"])

    assert talk.pending() == [], ("a second card was minted for the approved call", talk.pending())
    assert mpp._APPROVALS[card]["status"] == "consumed", mpp._APPROVALS[card]
    assert [args.get("token") for args in ran] == [MINT], ran
    assert "Manual mode requires approval" not in reply, reply


# --- what the resume slot keeps and still masks -----------------------------------------------------

EVM = "0x" + "ab" * 20


@pytest.mark.parametrize(("intent", "value"), [
    ("market.collect", MINT),
    ("market.collect", EVM),
    ("token.flow", MINT),
    ("web.chain_lookup", EVM),
    ("token.performance", MINT),
    ("token.research", "$BONK"),
])
def test_a_coin_named_by_token_is_kept_for_the_resume(intent, value) -> None:
    from core.agent_runtime.research_tool_loop_facade import resumable_pending_payload

    stored = resumable_pending_payload({"intent": intent, "arguments": {"token": value, "chain": "solana"}})
    assert stored["arguments"] == {"token": value, "chain": "solana"}


@pytest.mark.parametrize(("intent", "arguments"), [
    ("market.collect", {"api_token": MINT}),
    ("market.collect", {"access_token": "abc"}),
    ("market.collect", {"token_secret": MINT}),
    ("market.collect", {"api_key": MINT}),
    ("market.collect", {"token": "sk-live abc/def+ghi=="}),  # not shaped like a coin
    ("web.fetch", {"token": MINT}),                          # not a crypto tool
    ("x.post", {"token": "abc"}),
    ("", {"token": MINT}),
])
def test_secrets_stay_masked_in_the_resume_slot(intent, arguments) -> None:
    from core.agent_runtime.research_tool_loop_facade import resumable_pending_payload

    stored = resumable_pending_payload({"intent": intent, "arguments": dict(arguments)})
    assert set(stored["arguments"].values()) == {"[redacted]"}, stored


def test_events_and_receipts_still_mask_the_token_argument() -> None:
    """Only the resume slot changed: the event/receipt redactor masks `token` as before."""
    from core.agent_runtime.orchestrator import redact_tool_arguments

    assert redact_tool_arguments({"token": MINT, "chain": "solana"})["token"] == "[redacted]"
