"""Windows 0.7.1: the Hive is off until the owner opts in, and VOOL says so instead of offering it.

On the Windows seam (`core.ollama_cloud_models.CLOUD_REFUSAL_ACTIVE`, read by
`windows_cloud_gate.active()`), with a fresh profile (and also with `VOOL_HIVE_WATCHER_URL` set, which
is not an opt-in on Windows):

* the model's tool catalog does not offer `hive.list_available`;
* the capability ledger reports `hive.read` and `hive.write` as off by policy (`off_on_windows`,
  "The Hive is off on Windows in this release."), not "unavailable" or "unreachable";
* a swarm/Hive capability question says plainly that the Hive is off on Windows;
* a model call to `hive.list_available` answers locally (`disabled`, the same sentence), and a
  resolver and socket spy sees no name lookup and no connect.

Off Windows nothing changes: an unconfigured tracker still answers `not_configured` with the old
text, and the ledger still says `unavailable`.
"""
from __future__ import annotations

import socket

import pytest

from core import ollama_cloud_models
from core import hive_activity_tracker as hat
from core.public_hive_bridge import PublicHiveBridge, load_public_hive_bridge_config
from core.tool_intent_executor import (
    capability_truth_for_request,
    execute_tool_intent,
    render_capability_truth_response,
    runtime_capability_ledger,
    runtime_tool_specs,
)

OFF_TEXT = "The Hive is off on Windows in this release."


class _NetSpy:
    """Every name lookup and every connect, refused and recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []

        def refuse(kind: str):
            def call(*args, **kwargs):
                self.events.append(f"{kind}:{args[:2]!r}")
                raise OSError(f"no network in this test ({kind})")

            return call

        monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
        monkeypatch.setattr(socket, "gethostbyname", refuse("gethostbyname"))
        monkeypatch.setattr(socket, "gethostbyname_ex", refuse("gethostbyname_ex"))
        monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
        monkeypatch.setattr(socket.socket, "connect", lambda sock, *a, **k: refuse("connect")(*a, **k))
        monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, *a, **k: refuse("connect_ex")(*a, **k))


@pytest.fixture(params=["fresh-profile", "env-url-is-not-opt-in"])
def windows(request, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOOL_HIVE_FOLLOWUPS_ENABLED", raising=False)
    monkeypatch.delenv("VOOL_PUBLIC_HIVE_ENABLED", raising=False)
    if request.param == "env-url-is-not-opt-in":
        monkeypatch.setenv("VOOL_HIVE_WATCHER_URL", "https://watch.example.test")
    else:
        monkeypatch.delenv("VOOL_HIVE_WATCHER_URL", raising=False)
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", True)


def _call_hive_list_available() -> object:
    tracker = hat.HiveActivityTracker(config=hat.load_hive_activity_tracker_config())
    bridge = PublicHiveBridge(load_public_hive_bridge_config())
    return execute_tool_intent(
        {"intent": "hive.list_available", "arguments": {}},
        task_id="task-hive-off",
        session_id="session-hive-off",
        source_context={"operating_mode": "auto", "surface": "openclaw", "platform": "openclaw"},
        hive_activity_tracker=tracker,
        public_hive_bridge=bridge,
    )


def test_windows_the_catalog_does_not_offer_hive_list_available(windows) -> None:
    intents = {spec.get("intent") for spec in runtime_tool_specs()}
    assert "hive.list_available" not in intents


def test_windows_the_ledger_reports_the_hive_off_by_policy(windows) -> None:
    ledger = {entry["capability_id"]: entry for entry in runtime_capability_ledger()}
    read, write = ledger["hive.read"], ledger["hive.write"]

    assert read["supported"] is False and read["support_level"] == "unsupported"
    assert read["availability_state"] == "off_on_windows"
    assert read["gap_kind"] == "off_on_windows"
    assert read["unsupported_reason"] == OFF_TEXT
    assert read["dispatchable_intents"] == []
    assert write["supported"] is False
    assert write["gap_kind"] == "off_on_windows"
    assert write["unsupported_reason"] == OFF_TEXT


def test_windows_a_swarm_question_says_the_hive_is_off(windows) -> None:
    answer = render_capability_truth_response(
        capability_truth_for_request("can you delegate to other agents and merge helper outputs?")
    )
    assert answer.startswith("No. "), answer
    assert OFF_TEXT in answer, answer
    assert "partially" not in answer.lower(), answer


def test_windows_a_model_call_to_hive_list_available_answers_locally(windows, monkeypatch) -> None:
    spy = _NetSpy(monkeypatch)

    result = _call_hive_list_available()

    assert result.handled is True and result.ok is False
    assert result.status == "disabled"
    assert OFF_TEXT in result.response_text, result.response_text
    assert spy.events == [], spy.events


def test_off_windows_an_unconfigured_hive_call_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", False)
    monkeypatch.delenv("VOOL_PUBLIC_HIVE_ENABLED", raising=False)
    monkeypatch.setattr(
        "core.tool_intent_executor.load_hive_activity_tracker_config",
        lambda: hat.HiveActivityTrackerConfig(enabled=False, watcher_api_url=None),
    )
    spy = _NetSpy(monkeypatch)
    tracker = hat.HiveActivityTracker(config=hat.HiveActivityTrackerConfig(enabled=False, watcher_api_url=None))

    result = execute_tool_intent(
        {"intent": "hive.list_available", "arguments": {}},
        task_id="task-hive-off",
        session_id="session-hive-off",
        source_context={"operating_mode": "auto", "surface": "openclaw", "platform": "openclaw"},
        hive_activity_tracker=tracker,
    )
    ledger = {entry["capability_id"]: entry for entry in runtime_capability_ledger()}

    assert result.status == "not_configured"
    assert OFF_TEXT not in result.response_text
    assert ledger["hive.read"]["availability_state"] in {"unavailable", "configured_unverified"}
    assert ledger["hive.read"]["unsupported_reason"] == "Live Hive read actions are not configured on this runtime."
    assert spy.events == [], spy.events
