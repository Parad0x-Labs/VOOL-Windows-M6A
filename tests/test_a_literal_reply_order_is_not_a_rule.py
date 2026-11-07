"""An order for one literal reply ("Reply with the single word OK.") is not a standing rule, and saved rules are
listed on the Memory page.

Clean-VM job 52 (port 76afbd36): the tester typed "Reply with the single word OK." and VOOL answered "Saved for
every chat: Reply with the single word OK." The plain-rule law reads a whole turn of instructions as a rule when it
is about how answers look; "reply" is a style word, and "with" ended the object check as if the order had no object,
so the literal it asked for ("the single word OK", "yes or no", "just the number") was read as a presentation
setting. Every later chat in that workspace would then have carried "reply with the single word OK" as a standing
instruction. The Memory page listed nothing, because standing instructions are a separate store it never read, so
the owner could not see what was saved.
"""

from __future__ import annotations

import json

import pytest

from core import standing_instructions as si
from core.settings_extras_fragment import render_settings_extras_fragment
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get

OWNER = {"surface": "chat", "_owner_local": True, "workspace_root": "/srv/projects/harbour"}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_store_path", lambda: tmp_path / "standing_instructions.json")


def _say(text, context=OWNER):
    return si.observe_turn(text, context, principal="owner_local", session_id="chat:" + "c" * 20)


LITERAL_REPLY_ORDERS = [
    "Reply with the single word OK.",
    "Reply with the word OK.",
    "Answer with one word.",
    "Answer with a single word.",
    "Answer with yes or no.",
    "Reply with just yes or no.",
    "Answer with true or false.",
    "Reply with just the number.",
    "Reply with only the number.",
    "Reply with exactly OK.",
    "Respond with exactly done.",
    "Reply with only stored",
]


@pytest.mark.parametrize("text", LITERAL_REPLY_ORDERS)
def test_an_order_for_one_literal_reply_saves_nothing(text):
    assert _say(text)["saved"] == []
    assert si.standing_block(OWNER, principal="owner_local") == ""


@pytest.mark.parametrize("text", [
    "Always answer in bullet points.",
    "From now on reply in French.",
    "Keep answers short.",
    "Keep your answers under five sentences.",
    "Reply in short sentences.",
    "Reply with short answers.",
    "Answer with bullet points.",
    "Reply with one word answers.",
    "Respond with numbered answers.",
    "Give me answers in JSON.",
    # A standing word makes even a literal reply a rule the owner asked for by name.
    "Always reply with the single word OK.",
])
def test_a_genuine_rule_about_how_answers_look_is_still_saved(text):
    assert _say(text)["saved"] == [text]


def _memory_rules(**kwargs):
    response = dispatch_get(path="/api/memory/standing-instructions", query={},
                            runtime=RuntimeServices(display_name="VOOL"), model_name="vool", **kwargs)
    return response.status, json.loads(response.body)


def test_the_memory_page_route_lists_every_active_rule_with_its_workspace():
    other = {**OWNER, "workspace_root": "/srv/projects/lighthouse"}
    _say("Always answer in French.")
    _say("Use metric units.", other)
    _say("Always answer in English.")  # supersedes the French rule: same setting, same workspace

    status, body = _memory_rules(client_host="127.0.0.1")

    assert status == 200 and body["ok"] is True
    texts = [row["text"] for row in body["instructions"]]
    assert sorted(texts) == ["Always answer in English.", "Use metric units."]
    for row in body["instructions"]:
        assert row["id"].startswith("si-") and row["workspace"] and row["created_at"] > 0


def test_the_memory_page_route_is_owner_local():
    _say("Always answer in bullet points.")
    status, body = _memory_rules(client_host="203.0.113.9")
    assert status == 403 and body.get("ok") is False


def test_the_memory_page_shows_the_rules_and_how_to_remove_one():
    fragment = render_settings_extras_fragment()
    assert "/api/memory/standing-instructions" in fragment
    assert "vs-rule-list" in fragment
    for key in ("vx.memory.rules_title", "vx.memory.rules_empty", "vx.memory.rules_hint", "vx.memory.rules_unreadable"):
        assert key in fragment
