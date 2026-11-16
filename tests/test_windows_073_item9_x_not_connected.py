"""Windows: an X ask while X is not connected is answered by VOOL with one fixed line, no model round.

Steve, r072-4 (session openclaw:bd5f9a067a907d9088fd): "read my latest posts on X" seats x.user_posts,
but with no X keys the X contract is unsupported and the turn fell to the web lane: with Web lookups on
the model was handed web.search, with it off the reply was "Web lookups are off" (route
model_tool_intent_disabled), and with Local Only on it still blamed Web lookups (carry item 11).

On Windows the served chat now answers "X is not connected" and points at what Settings shows (API Keys,
the "X (Twitter)" row, its "Connect X" button), naming Local Only first when that is on. Off Windows the
turn is exactly as before (tests/test_x_turn_routing.py: nothing changes while X is not connected), and
with X connected nothing changes on any host.

Driven through the served /api/chat as the VOOL window sends it, with the model stubbed and counted.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.agent_runtime.windows_x_not_connected import (
    X_NOT_CONNECTED,
    X_NOT_CONNECTED_LOCAL_ONLY,
    maybe_handle_x_not_connected,
)
from tests.test_windows_run_in_folder_served_end_to_end import HEADERS, chat, machine

ASK = "read my latest posts on X"
WEB_OFF_REFUSAL = "Web lookups are off"


def _turn(open_chat, machine_dirs: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    from tests.asgi_harness import asgi_request

    session = open_chat(machine_dirs["desktop"])
    body = {
        "messages": [{"role": "user", "content": ASK}], "session_id": session.session, "stream": False,
        "mode": "manual", "turn_id": f"turn-{uuid.uuid4().hex[:10]}",
    }
    status, _headers, raw = asgi_request(
        session.app, method="POST", path="/api/chat", headers=HEADERS, body=json.dumps(body).encode("utf-8")
    )
    assert status == 200, raw[:400]
    payload = json.loads(raw.decode("utf-8") or "{}")
    reply = str((payload.get("message") or {}).get("content") or "")
    provenance = ((payload.get("vool_response_commit") or {}).get("display_metadata") or {}).get("provenance") or {}
    return session, reply, provenance


def _connect_x(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.x_api import connected
    from tests.test_x_api_reads import _connect

    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    _connect()
    assert connected()


def _local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.policy_engine as policy_engine

    monkeypatch.setattr(policy_engine, "local_only_mode", lambda *args, **kwargs: True)


def _web_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.policy_engine as policy_engine

    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda *args, **kwargs: True)


# --- served on Windows: the fixed reply, no model round -------------------------------------------------


@pytest.mark.parametrize("web_on", [False, True], ids=["web-off", "web-on"])
def test_windows_x_ask_without_keys_gets_the_fixed_reply_and_no_model_round(machine, chat, monkeypatch, web_on):
    if web_on:
        _web_on(monkeypatch)
    session, reply, provenance = _turn(chat, machine)

    assert reply == X_NOT_CONNECTED
    assert "Settings > API Keys > X (Twitter) > Connect X" in reply
    assert session.model_rounds == 0
    assert provenance.get("route") == "x_not_connected", provenance
    assert provenance.get("model_ran") is False
    assert WEB_OFF_REFUSAL not in reply


def test_windows_x_ask_under_local_only_names_local_only_not_web_lookups(machine, chat, monkeypatch):
    _local_only(monkeypatch)
    session, reply, provenance = _turn(chat, machine)

    assert reply == X_NOT_CONNECTED_LOCAL_ONLY
    assert reply.startswith("Local Only is on")
    assert "Web lookups" not in reply
    assert session.model_rounds == 0
    assert provenance.get("route") == "x_not_connected", provenance
    assert provenance.get("local_only") is True


# --- off Windows: exactly as before ---------------------------------------------------------------------


@pytest.mark.parametrize("local_only", [False, True], ids=["plain", "local-only"])
def test_off_windows_the_x_ask_without_keys_is_unchanged(machine, chat, monkeypatch, local_only):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    if local_only:
        _local_only(monkeypatch)
    session, reply, provenance = _turn(chat, machine)

    assert reply != X_NOT_CONNECTED and reply != X_NOT_CONNECTED_LOCAL_ONLY
    assert WEB_OFF_REFUSAL in reply
    assert provenance.get("route") == "model_tool_intent_disabled", provenance
    assert session.model_rounds == 0


# --- X connected: unchanged on every host ---------------------------------------------------------------


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "mac-linux"])
def test_with_x_connected_the_turn_still_reaches_the_model(machine, chat, monkeypatch, windows):
    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    session = chat(machine["desktop"])  # the VOOL home exists before the keys are saved into it
    _connect_x(monkeypatch)
    from tests.asgi_harness import asgi_request

    body = {
        "messages": [{"role": "user", "content": ASK}], "session_id": session.session, "stream": False,
        "mode": "manual", "turn_id": f"turn-{uuid.uuid4().hex[:10]}",
    }
    status, _headers, raw = asgi_request(
        session.app, method="POST", path="/api/chat", headers=HEADERS, body=json.dumps(body).encode("utf-8")
    )
    assert status == 200, raw[:400]
    reply = str((json.loads(raw.decode("utf-8")).get("message") or {}).get("content") or "")
    assert reply not in (X_NOT_CONNECTED, X_NOT_CONNECTED_LOCAL_ONLY)
    assert session.model_rounds >= 1


# --- the recognizer: only an X ask, only on Windows -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I read on X that rates are rising, what do you think?",
        "what's the price of $WIF",
        "write a tweet about my launch",
        "",
    ],
)
def test_passing_mentions_of_x_are_not_answered(monkeypatch, text):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert maybe_handle_x_not_connected(text) is None


def test_the_recognizer_stands_down_off_windows(monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert maybe_handle_x_not_connected(ASK) is None


def test_the_words_name_what_the_settings_page_shows():
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    res = dispatch_get(
        path="/settings", query={"ui_locale": ["en"]}, runtime=RuntimeServices(display_name="VOOL"),
        model_name="vool", client_host="127.0.0.1",
    )
    page = res.body.decode("utf-8") if isinstance(res.body, bytes) else res.body
    assert '"settings.group.keys.title":"API Keys"' in page
    assert "T('xsetup.title', 'X (Twitter)')" in page
    assert "T('xsetup.open', 'Connect X')" in page
