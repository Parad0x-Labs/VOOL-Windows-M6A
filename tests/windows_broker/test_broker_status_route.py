"""GET /api/windows/broker/status (Phase C slice C5, pick 7: the report the banner reads).

The route returns `session.report()` as it is. It names flagged folders, so it is an owner-local read
like the other local-data GETs: a request whose TCP peer is not loopback is refused with
`owner_local_required` and names no folder, and a rebound Host header is refused before any route.
The answer is never cached.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from core.web.api.app import create_api_app
from core.web.api.runtime import RuntimeServices
from core.windows_broker import session
from core.windows_broker.process import BrokerStatus, FlaggedFolder

PATH = "/api/windows/broker/status"
FOLDER = "C:\\work\\secret-project"


class _Host:
    status = BrokerStatus(True, "", r"\\.\pipe\vool-broker-test", 4242, (FlaggedFolder("a" * 48, FOLDER),),
                          {"available": True, "pending": [{"batch_id": "b1", "left": ["x.txt"]}], "torn": []})
    secret = b""


@pytest.fixture(autouse=True)
def _no_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_host", None)


def _get(client: str, host: str = "127.0.0.1:11435") -> httpx.Response:
    app = create_api_app(runtime=RuntimeServices(display_name="N"), model_name="vool")

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(client, 50123))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11435") as http:
            return await http.get(PATH, headers={"host": host})

    return asyncio.run(call())


def test_the_owner_reads_the_report_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_host", _Host())
    response = _get("127.0.0.1")
    assert response.status_code == 200
    assert response.json() == session.report()
    assert response.json()["flagged"] == [{"id": "a" * 48, "path": FOLDER}]
    assert response.json()["needs_check"] is True
    assert "no-store" in response.headers.get("cache-control", "")


def test_a_process_without_a_broker_reports_it_is_not_wired() -> None:
    response = _get("127.0.0.1")
    assert response.status_code == 200 and response.json() == {"wired": False}


@pytest.mark.parametrize("peer", ["203.0.113.9", "192.168.1.20", "testclient", ""])
def test_a_request_from_another_machine_is_refused_and_names_no_folder(monkeypatch: pytest.MonkeyPatch, peer: str) -> None:
    monkeypatch.setattr(session, "_host", _Host())
    response = _get(peer)
    assert response.status_code == 403
    body: Any = response.json()
    assert body.get("error") == "owner_local_required"
    assert "secret-project" not in json.dumps(body)


def test_a_rebound_host_header_is_refused_before_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_host", _Host())
    response = _get("127.0.0.1", host="attacker.example")
    assert response.status_code == 403
    assert "secret-project" not in response.text
