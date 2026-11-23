"""Windows: Settings > Memory reads a chat's policy and forget authority only when the chat owns a row.

Steve (0.7.3, 263 chats): the Memory page took 5.2-14.8 s. Every chat paid two SQLite opens for its
policy and a full forget-authority read (ledger, routing and lease files, three times over), even a chat
that owns no memory row, whose answer is already empty. On Windows a listing now skips both for such a
chat. A chat that owns a row gets every check as before, the listing's output is the same, and off
Windows nothing changes (0.7.3: Windows-only fixes).
"""
from __future__ import annotations

import json

import pytest


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


def _windows(monkeypatch, on: bool = True) -> None:
    from core import context_namespace
    from core.memory import entries

    monkeypatch.setattr(entries, "_SKIP_UNOWNED_ON", on)
    monkeypatch.setattr(context_namespace, "_SCHEMA_ONCE_ON", on)


def _chat(index: int) -> str:
    return "openclaw:" + "s%019d" % index


def _seed(count: int, *, facts: dict[int, str] | None = None, project_fact: int | None = None) -> None:
    from core.context_namespace import ensure_chat_namespace, grant_context_import
    from core.memory.entries import record_memory_entry, resolve_memory_access_policy

    for index in range(count):
        chat = _chat(index)
        ensure_chat_namespace(chat, project_id="proj_A" if project_fact is not None else None, grant_current_receipts=False)
        if project_fact is not None:
            grant_context_import(chat, scope="project", source_id="project:proj_A", source_project_id="proj_A")
        policy = resolve_memory_access_policy(chat_id=chat)
        fact = (facts or {}).get(index)
        if fact:
            assert record_memory_entry(fact, category="fact", session_id=chat, source="test", confidence=0.9,
                                       keywords=fact.lower().split()[:8], share_scope="local_only", scope="chat",
                                       authority="confirmed_memory", fact_key=None, expires_at=None, review_after=None,
                                       access_policy=policy)
        if index == project_fact:
            assert record_memory_entry("The Acme deploy window is Tuesday night.", category="fact", session_id=chat,
                                       source="test", confidence=0.9, keywords=["acme", "deploy", "window"],
                                       share_scope="local_only", project_id="proj_A", scope="project",
                                       authority="confirmed_memory", fact_key="acme_window", expires_at=None,
                                       review_after=None, access_policy=policy)


def _counting(monkeypatch) -> tuple[list[str], list[str]]:
    from core.memory import entries, revocation

    policies: list[str] = []
    authority: list[str] = []
    real_policy = entries.resolve_memory_access_policy
    real_tokens = revocation._one_reading_tokens

    def policy(*args, **kwargs):
        policies.append(str(kwargs.get("chat_id") or ""))
        return real_policy(*args, **kwargs)

    def tokens(state, chat_id):
        authority.append(str(chat_id or ""))
        return real_tokens(state, chat_id)

    monkeypatch.setattr(entries, "resolve_memory_access_policy", policy)
    monkeypatch.setattr(revocation, "_one_reading_tokens", tokens)
    return policies, authority


def _list() -> list[dict]:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    response = dispatch_get(path="/api/memory/entries", query={}, runtime=RuntimeServices(display_name="VOOL"),
                            model_name="vool", client_host="127.0.0.1")
    assert response.status == 200
    body = json.loads(response.body.decode())
    assert body["ok"] is True
    return body["entries"]


def test_chats_that_own_no_row_read_no_policy_and_no_forget_authority(home, monkeypatch) -> None:
    _windows(monkeypatch)
    _seed(12, facts={3: "My dog is called Biscuit."})
    policies, authority = _counting(monkeypatch)

    listed = _list()

    assert [row["fact"] for row in listed] == ["My dog is called Biscuit."]
    assert set(policies) == {_chat(3)}  # before: all 12 chats
    assert set(authority) <= {_chat(3)} and authority  # before: every chat's forget authority


def test_the_listing_is_the_same_on_and_off_the_windows_path(home, monkeypatch) -> None:
    _seed(6, facts={1: "My dog is called Biscuit.", 4: "I take the 7:40 train."})
    _windows(monkeypatch, on=False)
    shipped = _list()
    _windows(monkeypatch, on=True)

    assert _list() == shipped
    assert sorted(row["fact"] for row in shipped) == ["I take the 7:40 train.", "My dog is called Biscuit."]


def test_a_project_row_still_reads_every_chats_policy_and_authority(home, monkeypatch) -> None:
    """A project row is every chat's to read, so no chat is skipped and each one still gets every check."""
    _windows(monkeypatch)
    _seed(4, project_fact=0)
    policies, authority = _counting(monkeypatch)

    listed = _list()

    assert [row["fact"] for row in listed] == ["The Acme deploy window is Tuesday night."]
    assert set(policies) == {_chat(index) for index in range(4)}
    assert set(authority) == {_chat(index) for index in range(4)}


def test_a_forgotten_fact_stays_hidden_on_the_windows_path(home, monkeypatch) -> None:
    from core.memory import revocation

    _windows(monkeypatch)
    _seed(5, facts={2: "My dog is called Biscuit."})
    monkeypatch.setattr(revocation, "_one_reading_tokens", lambda state, chat_id: ("Biscuit",))

    assert _list() == []


def test_off_windows_every_chat_still_reads_its_policy_as_mac_ships(home, monkeypatch) -> None:
    _windows(monkeypatch, on=False)
    _seed(5, facts={0: "My dog is called Biscuit."})
    policies, _authority = _counting(monkeypatch)

    _list()

    assert set(policies) == {_chat(index) for index in range(5)}
