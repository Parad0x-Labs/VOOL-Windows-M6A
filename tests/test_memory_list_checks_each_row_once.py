"""Settings > Memory: one listing checks each remembered row once, not once per chat.

Seen on Steve (0.7.1): the Memory page took about 14 s to show. GET /api/memory/entries reads every
active chat (the Settings window has no chat of its own) and listed each chat separately. Each
listing ran the full governed check on EVERY row in the store -- the secret scan, the A8 erase
verdict (a SQLite open) and the origin chat's namespace (two more opens) -- before the cheap test
that most rows belong to another chat. So the work was chats x rows x three store opens: 30 chats
and 600 rows took 85 s on Linux. These tests pin what the listing shows and that the expensive
checks no longer multiply by the number of chats.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

CHATS = 6


def _row(chat: str, text: str, *, scope: str = "chat", expires_at: str = "", created: str) -> dict:
    rid = f"memory-{uuid.uuid4().hex}"
    return {
        "record_id": rid, "created_at": created, "request_id": "", "last_confirmed_at": created,
        "text": text, "category": "fact", "fact_key": "fact:" + rid, "scope": scope,
        "origin_chat_id": chat, "origin_project_id": "", "session_id": chat, "project_id": "",
        "source": "conversation", "source_id": "profile:confirmed" if scope == "user_profile" else rid,
        "status": "active", "authority": "confirmed_memory", "superseded_record_id": "",
        "expires_at": expires_at, "review_after": "",
        "provenance": {"kind": "conversation", "source_id": rid,
                       "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                       "origin_chat_id": chat, "origin_project_id": "", "recorded_at": created},
        "confidence": 0.9, "keywords": ["fact"], "share_scope": "local_only",
    }


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "home" / "workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    try:
        yield tmp_path / "home"
    finally:
        runtime_paths.configure_runtime_home(None)


def _seed():
    from core.context_namespace import ensure_chat_namespace, grant_context_import, set_chat_namespace_state
    from core.memory.entries import _ensure_memory_files, resolve_memory_access_policy
    from core.memory.files import memory_entries_path
    from core.persistent_memory import append_conversation_event

    chats = []
    for index in range(CHATS):
        chat = "openclaw:" + "m%019d" % index
        ensure_chat_namespace(chat, grant_current_receipts=False)
        append_conversation_event(session_id=chat, user_input=f"Hello {index}", assistant_output="Hi.",
                                  access_policy=resolve_memory_access_policy(chat_id=chat))
        chats.append(chat)
    # Two chats may read the profile; the rest may not.
    for chat in chats[:2]:
        grant_context_import(chat, scope="user_profile", source_id="profile:confirmed")
    archived = "openclaw:" + "a%019d" % 1
    ensure_chat_namespace(archived, grant_current_receipts=False)
    _ensure_memory_files()
    rows, expect = [], set()
    minute = 0

    def stamp():
        nonlocal minute
        minute += 1
        return "2026-11-10T10:%02d:00Z" % minute

    for index, chat in enumerate(chats):
        for n in range(3):
            row = _row(chat, f"Chat {index} keeps the boat named Juniper{index}x{n}.", created=stamp())
            rows.append(row)
            expect.add(row["record_id"])
    for n in range(4):
        row = _row(chats[3], f"The owner prefers green tea number {n}.", scope="user_profile", created=stamp())
        rows.append(row)
        expect.add(row["record_id"])   # listed through the chats that hold the profile grant
    withheld = [
        _row(chats[0], "An expired note about the ferry.", expires_at="2020-01-01T00:00:00+00:00", created=stamp()),
        _row(chats[1], "my key is sk-ant-api03-" + "Q" * 90, created=stamp()),
        _row(archived, "A fact from a chat that was archived.", created=stamp()),
        {**_row(chats[2], "A legacy row with no provenance.", created=stamp()), "provenance": None},
    ]
    rows.extend(withheld)
    with memory_entries_path().open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    set_chat_namespace_state(archived, "archived")
    return chats, rows, expect, {row["record_id"] for row in withheld}


def _list():
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    response = dispatch_get(path="/api/memory/entries", query={"limit": ["200"]},
                            runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                            client_host="127.0.0.1")
    assert response.status == 200
    body = json.loads(response.body.decode())
    assert body["ok"] is True
    return body["entries"]


def test_the_memory_listing_shows_exactly_the_governed_rows(home) -> None:
    _chats, _rows, expect, withheld = _seed()
    listed = {entry["record_id"] for entry in _list()}
    assert listed == expect, (sorted(listed - expect), sorted(expect - listed))
    assert not listed & withheld


def test_each_row_is_checked_once_per_listing_not_once_per_chat(home, monkeypatch) -> None:
    from core import context_namespace, finalization
    from core.memory import entries

    _chats, rows, expect, _withheld = _seed()
    _list()   # first read may set up the stores
    erase_checks, namespace_loads = [], []
    real_writer = finalization.writer_may_persist_text
    real_load = context_namespace.load_chat_namespace

    def counting_writer(text):
        erase_checks.append(text)
        return real_writer(text)

    def counting_load(chat_id):
        namespace_loads.append(chat_id)
        return real_load(chat_id)

    monkeypatch.setattr(finalization, "writer_may_persist_text", counting_writer)
    monkeypatch.setattr(entries, "load_chat_namespace", counting_load)
    listed = {entry["record_id"] for entry in _list()}
    assert listed == expect
    # Before: every row for every chat (len(rows) * CHATS erase checks, as many namespace loads).
    assert len(erase_checks) <= len(rows), f"{len(erase_checks)} erase checks for {len(rows)} rows"
    assert len(namespace_loads) <= len(rows) + 2 * CHATS, (
        f"{len(namespace_loads)} namespace loads for {len(rows)} rows and {CHATS} chats"
    )


def test_a_single_chat_listing_is_unchanged(home) -> None:
    from core.memory.entries import list_memory_entries

    chats, _rows, _expect, withheld = _seed()
    own = {row["record_id"] for row in list_memory_entries(chat_id=chats[2], limit=200)}
    assert len(own) == 3 and not own & withheld
    with_profile = {row["text"] for row in list_memory_entries(chat_id=chats[0], limit=200)}
    assert sum("green tea" in text for text in with_profile) == 4
    assert not any("ferry" in text for text in with_profile)
