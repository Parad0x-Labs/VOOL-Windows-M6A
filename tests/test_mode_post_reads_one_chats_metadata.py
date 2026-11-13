"""POST /api/mode reads the ONE chat's metadata it needs, not every chat's.

Seen on Steve: changing a chat's mode took seconds (3-23 s in round 22, still slow in 0.7.1).
Every mode POST resolves the chat's server-owned workspace (core/context_namespace.py
authoritative_chat_workspace). For a chat not bound to a project in its namespace -- most chats --
that fell back to ``load_session_meta()``, which reads and forget-authorizes the metadata of EVERY
chat (three authority reads per chat, each opening the memory store twice) only to look up one
chat's project id. So one mode change cost a multiple of the number of chats. These tests pin
that only the asked-for chat's metadata is authorized, and that the fallback still resolves a
project bound through metadata and still fails closed when that row is withheld.
"""
from __future__ import annotations

import json

import pytest

CHATS = 8


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "home" / "workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    try:
        yield tmp_path
    finally:
        runtime_paths.configure_runtime_home(None)


def _chat(index: int) -> str:
    return "openclaw:" + "d%019d" % index


def _seed() -> list[str]:
    from core.context_namespace import ensure_chat_namespace
    from core.memory.entries import resolve_memory_access_policy, set_session_meta
    from core.persistent_memory import append_conversation_event

    chats = []
    for index in range(CHATS):
        chat = _chat(index)
        ensure_chat_namespace(chat, grant_current_receipts=False)
        append_conversation_event(session_id=chat, user_input=f"Hello {index}", assistant_output="Hi.",
                                  access_policy=resolve_memory_access_policy(chat_id=chat))
        set_session_meta(chat, title=f"Chat {index}")
        chats.append(chat)
    return chats


def _post_mode(body: dict):
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    return dispatch_post(path="/api/mode", body=body, headers={"content-type": "application/json"},
                         runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                         workspace_root_provider=lambda: "", client_host="127.0.0.1")


def _authorized_chats(monkeypatch) -> list[str]:
    from core.memory import revocation

    seen: list[str] = []
    real = revocation.authorize_payload

    def counting(payload, chat_id=None, *args, **kwargs):
        seen.append(str(chat_id or ""))
        return real(payload, chat_id, *args, **kwargs)

    monkeypatch.setattr(revocation, "authorize_payload", counting)
    return seen


@pytest.mark.parametrize("op", ["set", "bypass_options"])
def test_a_mode_post_authorizes_only_the_chat_it_names(home, monkeypatch, op) -> None:
    chats = _seed()
    target = chats[3]
    seen = _authorized_chats(monkeypatch)
    body = {"op": op, "session_id": target, "mode": "plan"}
    response = _post_mode(body)
    assert response.status == 200, response.body
    others = sorted({chat for chat in seen if chat and chat != target})
    assert not others, f"POST /api/mode op={op} authorized {len(others)} other chats' metadata: {others[:3]}"


def test_a_project_bound_only_in_metadata_is_still_the_chats_workspace(home) -> None:
    from core import project_store
    from core.context_namespace import authoritative_chat_workspace, load_chat_namespace
    from core.memory.entries import _load_session_meta_raw, _write_session_meta_atomic

    chats = _seed()
    folder = home / "proj"
    folder.mkdir()
    ok, project = project_store.create_project("Proj", str(folder))
    assert ok, project
    meta = _load_session_meta_raw()
    meta[chats[2]] = {**meta.get(chats[2], {}), "project_id": project["id"]}
    _write_session_meta_atomic(meta)
    assert not (load_chat_namespace(chats[2]).project_id or "")   # the namespace does not carry it
    root, reason = authoritative_chat_workspace(chats[2])
    assert reason == "project" and root == project["root"]
    body = json.loads(_post_mode({"op": "bypass_options", "session_id": chats[2]}).body.decode())
    assert body["until_off_available"] is True and body["workspace_reason"] == "project"
    assert authoritative_chat_workspace(chats[1]) == ("", "unbound")


def test_a_withheld_metadata_row_still_fails_closed(home, monkeypatch) -> None:
    from core import project_store
    from core.context_namespace import authoritative_chat_workspace
    from core.memory import revocation
    from core.memory.entries import _load_session_meta_raw, _write_session_meta_atomic

    chats = _seed()
    folder = home / "proj"
    folder.mkdir()
    _ok, project = project_store.create_project("Proj", str(folder))
    meta = _load_session_meta_raw()
    meta[chats[2]] = {**meta.get(chats[2], {}), "project_id": project["id"]}
    _write_session_meta_atomic(meta)
    real = revocation.authorize_payload

    def refuse_that_chat(payload, chat_id=None, *args, **kwargs):
        if chat_id == chats[2]:
            return False
        return real(payload, chat_id, *args, **kwargs)

    monkeypatch.setattr(revocation, "authorize_payload", refuse_that_chat)
    assert authoritative_chat_workspace(chats[2]) == ("", "unbound")
