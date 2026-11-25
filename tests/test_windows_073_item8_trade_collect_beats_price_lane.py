"""Windows: "collect the last 7 days of trades for $BONK on Solana" reaches market.collect, not a price quote.

Steve r072-4 (14 Nov, session openclaw:cf9818417c1e25ca3a75): the ask seats market.collect
(core.tool_demand_signals), but `_live_data_classification` also read it as a price ask (the `$BONK`
notation, and "trades" is a market word), so with a real model route the live-data lane
(live_data_typed_plan) answered with a CoinGecko quote and nothing was collected. On Windows the lane
now stands down when market.collect is demanded and no price is asked beside it, and the turn's
contract requires tools, so the tool loop gets it with market.collect on offer. Price asks, a price
asked beside a collect ask, and buy/sell questions (token.flow) classify exactly as before; off
Windows nothing changes.

Served end to end through /api/chat with the fixtures of
tests/test_windows_run_in_folder_served_end_to_end.py; the model is stubbed.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.execution_requirements import _live_data_classification, requirements_for
from tests.asgi_harness import asgi_request
from tests.test_windows_run_in_folder_served_end_to_end import HEADERS, chat, machine
from tools.web import coin_index

COLLECT = "collect the last 7 days of trades for $BONK on Solana"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
COLLECT_ASKS = (
    COLLECT,
    "Collect the last 7 days of trades for $BONK on Solana.",
    "pull the candles for $BTC",
    "collect the last week of trades for " + BONK_MINT,
)
PRICE_ASKS = (
    "price of $BONK",
    "what is the price of $BONK",
    "$BONK price",
    "how is $BONK trading today",
    "btc price",
    "price of $BONK and collect the last 7 days of trades for $BONK",
)
FLOW_ASKS = ("is $BONK being bought or sold right now", "what is the buy pressure on $BONK")


@pytest.fixture
def bonk_index(monkeypatch: pytest.MonkeyPatch):
    """The coin index knows BONK, as Steve's cached index did."""
    monkeypatch.setattr(coin_index, "_memory_index", {"bonk": "bonk", "btc": "bitcoin"}, raising=False)
    monkeypatch.setattr(coin_index, "_memory_stamp", time.time(), raising=False)
    yield
    coin_index.reset_cache_for_test()


def _on(windows: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)


def _shape(text: str) -> tuple[Any, ...]:
    req = requirements_for(text)
    return (req.answer_mode, req.tools_required, req.allowed_toolsets, req.reason_codes, _live_data_classification(text))


# --- the classification -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", COLLECT_ASKS)
def test_on_windows_a_collect_ask_is_not_a_price_ask(text, bonk_index, monkeypatch) -> None:
    _on(True, monkeypatch)
    assert _live_data_classification(text) is None
    req = requirements_for(text)
    assert req.answer_mode != "LIVE_DATA" and "market_prices" not in req.allowed_toolsets, req
    assert req.tools_required and "market_collect_request" in req.reason_codes, req


def test_off_windows_the_collect_ask_classifies_as_before(bonk_index, monkeypatch) -> None:
    _on(False, monkeypatch)
    assert _live_data_classification(COLLECT) == (False, False, ("market_prices",))
    assert requirements_for(COLLECT).answer_mode == "LIVE_DATA"


@pytest.mark.parametrize("text", PRICE_ASKS + FLOW_ASKS)
def test_price_and_flow_asks_classify_the_same_on_windows(text, bonk_index, monkeypatch) -> None:
    _on(False, monkeypatch)
    before = _shape(text)
    _on(True, monkeypatch)
    assert _shape(text) == before


@pytest.mark.parametrize("text", PRICE_ASKS[:4])
def test_a_price_ask_still_goes_to_the_live_data_lane(text, bonk_index, monkeypatch) -> None:
    _on(True, monkeypatch)
    req = requirements_for(text)
    assert req.answer_mode == "LIVE_DATA" and "market_prices" in req.allowed_toolsets, req


def test_the_collect_contract_offers_market_collect(bonk_index, monkeypatch) -> None:
    from core.capability_graph import model_visible_specs

    _on(True, monkeypatch)
    req = requirements_for(COLLECT)
    offered = [spec["intent"] for spec in model_visible_specs(user_text=COLLECT, toolset_hints=req.allowed_toolsets)]
    assert "market.collect" in offered, offered


# --- served end to end ------------------------------------------------------------------------------


def _model_calls_market_collect(talk, monkeypatch) -> list[dict[str, Any]]:
    """The model asks for market.collect on its first round and answers in prose after."""
    from core.memory_first_router import ModelExecutionDecision

    rounds: list[dict[str, Any]] = []

    def decide(**kwargs: Any) -> ModelExecutionDecision:
        rounds.append(kwargs)
        out = (
            {"intent": "market.collect", "arguments": {"token": BONK_MINT, "chain": "solana", "window": "7d"}}
            if len(rounds) == 1
            else {"intent": "respond.direct", "arguments": {"message": "Collected."}}
        )
        return ModelExecutionDecision(
            source="provider_execution", task_hash="t-collect", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=out,
            confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )

    monkeypatch.setattr(talk.agent.memory_router, "resolve_tool_intent", decide)
    return rounds


def _post(talk, text: str, *, turn_id: str, mode: str = "manual") -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": text}], "session_id": talk.session, "stream": False,
        "mode": mode, "turn_id": turn_id,
    }
    status, _headers, raw = asgi_request(
        talk.app, method="POST", path="/api/chat", headers=HEADERS, body=json.dumps(body).encode("utf-8")
    )
    assert status == 200, raw[:400]
    return json.loads(raw.decode("utf-8"))


def _route(payload: dict[str, Any]) -> str:
    return str(payload["vool_response_commit"]["display_metadata"]["provenance"]["route"])


def test_served_the_collect_ask_reaches_market_collect_on_windows(machine, chat, bonk_index, monkeypatch) -> None:
    """Manual mode: the model's market.collect call is the one the approval card asks about."""
    talk = chat(machine["desktop"])
    rounds = _model_calls_market_collect(talk, monkeypatch)

    payload = _post(talk, COLLECT, turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert _route(payload) != "live_data_typed_plan", payload["message"]["content"]
    assert "| Asset | Price |" not in payload["message"]["content"]
    assert rounds, "the turn never reached the model's tool loop"
    pending = talk.pending()
    assert len(pending) == 1 and pending[0]["intent"] == "market.collect", pending


def test_served_the_collect_ask_runs_market_collect_on_windows(machine, chat, bonk_index, monkeypatch) -> None:
    """Auto mode: market.collect runs. Web lookups are off in the test home, so the tool's own refusal is in
    the reply (the stubbed model writes no synthesis, so the runtime shows the tool result as is)."""
    talk = chat(machine["desktop"])
    rounds = _model_calls_market_collect(talk, monkeypatch)

    payload = _post(talk, COLLECT, turn_id=f"turn-{uuid.uuid4().hex[:10]}", mode="auto")
    reply = str(payload["message"]["content"])

    assert rounds and _route(payload) != "live_data_typed_plan", (_route(payload), reply)
    assert "Collecting trades uses public DEX candles" in reply and "| Asset | Price |" not in reply, reply


def test_served_the_collect_ask_off_windows_is_unchanged(machine, chat, bonk_index, monkeypatch) -> None:
    talk = chat(machine["desktop"])
    _on(False, monkeypatch)
    rounds = _model_calls_market_collect(talk, monkeypatch)

    payload = _post(talk, COLLECT, turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert _route(payload) == "live_data_typed_plan" and not rounds, payload["message"]["content"]


@pytest.mark.parametrize("text", ["price of $BONK", "what is the price of $BONK"])
def test_served_a_price_ask_routes_the_same_on_windows(text, machine, chat, bonk_index, monkeypatch) -> None:
    routes = {}
    for windows in (False, True):
        _on(windows, monkeypatch)
        talk = chat(machine["desktop"])
        payload = _post(talk, text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")
        routes[windows] = (_route(payload), payload["message"]["content"])
    assert routes[True] == routes[False], routes
