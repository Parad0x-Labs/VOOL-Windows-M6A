"""With Settings > Web lookups off, a price, weather or sea-temperature question makes no network attempt.

Measured on Steve, 14 Nov 2026 (0.7.2 at 0360e41e): with Web lookups off, "collect the last 7 days of trades
for $ BONK on Solana" went to CoinGecko through the live-data runner, which never asked the switch. Settings
says "nothing goes online for lookups until you switch this on". The switch is now asked at the runner (a
plain refusal) and inside each fetch function (so no other caller can miss it). An unreadable policy counts
as off; with the switch on, the same calls still reach their (stubbed) provider.
"""
from __future__ import annotations

import pytest

from core import policy_engine
from core.agent_runtime import live_data_runner
from core.agent_runtime.live_data_plan import SubtaskLifecycle
from core.live_data_plan import _market_subtask, _water_subtask, _weather_subtask
from core.remote_fetch_policy import RemoteFetchRefusedError

_SUBTASKS = (
    _market_subtask("rows", "bitcoin", "Bitcoin", kind="crypto"),
    _weather_subtask("rows", "Paris"),
    _water_subtask("rows", "Nice"),
)


@pytest.fixture
def sockets(monkeypatch):
    """Every way these lookups reach the network, recorded instead of run."""
    attempts: list[str] = []

    def refuse(*args, **kwargs):
        attempts.append(str(args[0] if args else kwargs))
        raise OSError("network attempted in a Web-lookups-off row")

    import urllib.request

    from core import remote_fetch_policy

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    if hasattr(remote_fetch_policy, "open_remote"):
        monkeypatch.setattr(remote_fetch_policy, "open_remote", refuse)
    import socket

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: refuse(*a, **k))
    return attempts


def _off(monkeypatch):
    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: False)


def _unreadable(monkeypatch):
    def boom():
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(policy_engine, "allow_web_fallback", boom)


@pytest.mark.parametrize("switch", [_off, _unreadable], ids=["off", "unreadable"])
@pytest.mark.parametrize("subtask", _SUBTASKS, ids=lambda s: s.operation)
def test_the_runner_refuses_before_any_network_attempt(monkeypatch, sockets, switch, subtask) -> None:
    switch(monkeypatch)
    outcome = live_data_runner._run_one(subtask, timeout_s=1.0)
    assert outcome.state is SubtaskLifecycle.FAILED
    assert outcome.failure_reason == live_data_runner.WEB_LOOKUPS_OFF
    assert sockets == []


@pytest.mark.parametrize("switch", [_off, _unreadable], ids=["off", "unreadable"])
def test_each_fetch_function_refuses_on_its_own(monkeypatch, sockets, switch) -> None:
    from core.fresh_data.water_temperature import water_temperature_lookup
    from tools.web import web_research

    switch(monkeypatch)
    target = web_research._MARKET_QUOTE_TARGETS[0]
    calls = (
        lambda: web_research._crypto_price_fallback_multi(["bitcoin"], timeout_s=1.0),
        lambda: web_research._market_quote_fallback_multi("", [target], timeout_s=1.0),
        lambda: web_research.structured_weather_lookup("Paris", timeout_s=1.0),
        lambda: water_temperature_lookup("Nice", timeout_s=1.0),
    )
    for call in calls:
        with pytest.raises(RemoteFetchRefusedError, match="Web lookups is off"):
            call()
    assert sockets == []


def test_with_the_switch_on_the_runner_still_reaches_its_provider(monkeypatch) -> None:
    from tools.web import web_research

    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: True)
    asked: list[list[str]] = []

    def provider(coin_ids, *, timeout_s):
        asked.append(list(coin_ids))
        return []

    monkeypatch.setattr(web_research, "_crypto_price_fallback_multi", provider)
    outcome = live_data_runner._run_one(_SUBTASKS[0], timeout_s=1.0)
    assert asked == [["bitcoin"]]
    assert outcome.failure_reason == "no quote returned"


def test_the_conductor_market_node_makes_no_network_attempt_with_the_switch_off(monkeypatch, sockets) -> None:
    from types import SimpleNamespace

    from core.conductor import operations

    _off(monkeypatch)
    monkeypatch.setattr(operations, "_turn_forbids", lambda node, ctx, toolset: False)
    node = SimpleNamespace(arguments={"asset_key": "bitcoin", "entity": "Bitcoin", "kind": "crypto"})
    with pytest.raises(ConnectionError, match="Web lookups is off"):
        operations._market_run(node, SimpleNamespace(timeout_s=1.0))
    assert sockets == []
