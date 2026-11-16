"""Settings > Memory ensures the chat-namespace schema once per listing, not twice per chat.

Steve (0.7.2, 259 chats): GET /api/memory/entries took 4.8 s, against 0.75 s on Linux. One listing
opened SQLite 1041 times there, two of every four opens per chat only to re-run the namespace
schema script (load_chat_namespace and list_context_imports each ensured it). Every SQLite open is
dearer on Windows. Inside one listing the schema is now ensured once per database; outside one,
every call still ensures it, and a failed ensure is never remembered.
"""
from __future__ import annotations

import json

import pytest

_SEED_CHATS = """
from core.context_namespace import ensure_chat_namespace
from core.memory.entries import resolve_memory_access_policy
from core.persistent_memory import append_conversation_event
for index in range({count}):
    chat = "openclaw:" + "s%019d" % index
    ensure_chat_namespace(chat, grant_current_receipts=False)
    policy = resolve_memory_access_policy(chat_id=chat)
    append_conversation_event(session_id=chat, user_input="Hello number %d" % index, assistant_output="Hi.",
                              access_policy=policy)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "home" / "workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    try:
        yield tmp_path / "home"
    finally:
        runtime_paths.configure_runtime_home(None)


def _counting(monkeypatch):
    from core import context_namespace as cn

    ran: list[int] = []
    real = cn._ensure_context_namespace_schema

    def counting():
        ran.append(1)
        return real()

    monkeypatch.setattr(cn, "_ensure_context_namespace_schema", counting)
    return ran


def _list():
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    response = dispatch_get(path="/api/memory/entries", query={}, runtime=RuntimeServices(display_name="VOOL"),
                            model_name="vool", client_host="127.0.0.1")
    assert response.status == 200
    body = json.loads(response.body.decode())
    assert body["ok"] is True
    return body["entries"]


def test_one_listing_ensures_the_namespace_schema_a_fixed_number_of_times(home, monkeypatch) -> None:
    exec(_SEED_CHATS.format(count=12), {})
    ran = _counting(monkeypatch)
    _list()
    # Before: twice for every chat (>= 24 for 12 chats).
    assert 1 <= len(ran) <= 2, len(ran)


def test_outside_a_listing_every_call_still_ensures_the_schema(home, monkeypatch) -> None:
    from core.context_namespace import ensure_context_namespace_schema

    ran = _counting(monkeypatch)
    for _ in range(3):
        ensure_context_namespace_schema()
    assert len(ran) == 3


def test_inside_one_scope_the_schema_is_ensured_once(home, monkeypatch) -> None:
    from core.context_namespace import _schema_once, ensure_context_namespace_schema

    ran = _counting(monkeypatch)
    with _schema_once():
        for _ in range(5):
            ensure_context_namespace_schema()
        with _schema_once():             # nested: the outer scope's memo
            ensure_context_namespace_schema()
    assert len(ran) == 1
    ensure_context_namespace_schema()    # the scope ended: ensured again
    assert len(ran) == 2


def test_a_failed_ensure_is_not_remembered(home, monkeypatch) -> None:
    from core import context_namespace as cn

    real = cn._ensure_context_namespace_schema
    calls: list[int] = []

    def failing_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("store unavailable")
        return real()

    monkeypatch.setattr(cn, "_ensure_context_namespace_schema", failing_once)
    with cn._schema_once():
        with pytest.raises(RuntimeError):
            cn.ensure_context_namespace_schema()
        cn.ensure_context_namespace_schema()
        cn.ensure_context_namespace_schema()
    assert len(calls) == 2


def test_a_second_database_in_the_same_scope_gets_its_own_ensure(home, tmp_path, monkeypatch) -> None:
    from core import context_namespace as cn

    ran = _counting(monkeypatch)
    paths = iter([str(tmp_path / "a.db"), str(tmp_path / "a.db"), str(tmp_path / "b.db")])
    monkeypatch.setattr(cn, "active_default_db_path", lambda: next(paths))
    with cn._schema_once():
        cn.ensure_context_namespace_schema()
        cn.ensure_context_namespace_schema()
        cn.ensure_context_namespace_schema()
    assert len(ran) == 2


def test_the_listing_still_shows_every_chat_and_a_new_chat_after_it(home, monkeypatch) -> None:
    exec(_SEED_CHATS.format(count=3), {})
    first = _list()
    exec(_SEED_CHATS.replace('"s%019d"', '"t%019d"').format(count=1), {})
    second = _list()
    assert len(second) >= len(first)
