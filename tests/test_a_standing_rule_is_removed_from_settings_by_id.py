"""The Settings page's Remove button takes back exactly one standing instruction, by id.

POST /api/memory/standing-instructions/remove {id} makes the same row update a chat take-back makes
(status "taken_back"), so the rule is gone from the very next turn's standing block while every other
rule stays. Owner-local: a non-loopback caller gets 403 and nothing changes; an id that names no active
rule gets 404. The rules are saved through the real owner turn reader (observe_turn), not written by hand.
"""
from __future__ import annotations

import json

import pytest

from core import standing_instructions as si

OWNER = {"surface": "chat", "_owner_local": True, "workspace_root": "/srv/projects/atlas"}
KM_RULE = "From now on, give me distances in kilometres."
SUMMARY_RULE = "Always end your reply with a one-line summary."


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    path = tmp_path / "standing_instructions.json"
    monkeypatch.setattr(si, "_store_path", lambda: path)
    return path


def _save_two_rules() -> dict[str, str]:
    for text in (KM_RULE, SUMMARY_RULE):
        assert si.observe_turn(text, OWNER, principal="owner_local")["saved"], text
    return {item.text: item.instruction_id for item in si.list_active()}


def _remove(body: dict, *, client_host: str = "127.0.0.1", headers: dict | None = None) -> tuple[int, dict]:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    response = dispatch_post(
        path="/api/memory/standing-instructions/remove",
        body=body,
        headers={"content-type": "application/json", **(headers or {})},
        runtime=RuntimeServices(display_name="VOOL"),
        model_name="vool",
        workspace_root_provider=lambda: "/srv/projects/atlas",
        client_host=client_host,
    )
    return response.status, json.loads(response.body.decode("utf-8"))


def _block() -> str:
    return si.standing_block(OWNER, principal="owner_local")


def test_removing_a_rule_by_id_takes_it_out_of_the_next_turn_and_keeps_the_others(_store):
    ids = _save_two_rules()
    assert KM_RULE in _block() and SUMMARY_RULE in _block()

    status, payload = _remove({"id": ids[KM_RULE]})
    assert status == 200, payload
    assert payload["ok"] is True and payload["removed"] is True and payload["text"] == KM_RULE

    block = _block()
    assert KM_RULE not in block
    assert SUMMARY_RULE in block
    assert [item.text for item in si.list_active()] == [SUMMARY_RULE]
    # The row is taken back, as a chat take-back leaves it, not deleted.
    rows = json.loads(_store.read_text(encoding="utf-8"))["instructions"]
    gone = next(row for row in rows if row["id"] == ids[KM_RULE])
    assert gone["status"] == "taken_back" and gone["taken_back_at"] > 0

    # A second press on the same id names no active rule any more.
    status, payload = _remove({"id": ids[KM_RULE]})
    assert status == 404 and payload["ok"] is False, payload
    assert SUMMARY_RULE in _block()


def test_a_caller_that_is_not_on_this_computer_gets_403_and_nothing_changes():
    ids = _save_two_rules()
    status, payload = _remove({"id": ids[KM_RULE]}, client_host="192.168.1.40")
    assert status == 403 and payload["error"] == "owner_local_required", payload
    assert KM_RULE in _block() and SUMMARY_RULE in _block()


def test_a_cross_origin_page_gets_403_and_nothing_changes():
    ids = _save_two_rules()
    status, _ = _remove({"id": ids[KM_RULE]}, headers={"origin": "https://evil.example"})
    assert status == 403
    assert KM_RULE in _block()


def test_an_unknown_id_gets_404_and_nothing_changes():
    _save_two_rules()
    status, payload = _remove({"id": "si-000000000000"})
    assert status == 404 and payload == {"ok": False, "error": "unknown_instruction"}
    assert KM_RULE in _block() and SUMMARY_RULE in _block()


@pytest.mark.parametrize("body", [{}, {"id": ""}, {"id": 7}, {"id": "x", "workspace": "default"}])
def test_a_malformed_body_is_refused_and_removes_nothing(body):
    _save_two_rules()
    status, _ = _remove(body)
    assert status == 400
    assert len(si.list_active()) == 2
