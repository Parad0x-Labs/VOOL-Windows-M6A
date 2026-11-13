"""A standing instruction given in a chat with no project, on the default folder, stays in that chat.

Every unbound chat gets the same default folder (the Desktop in the installed app, ~/Desktop on a Mac), so keying a
rule by that folder carried "always end answers with OK-TEST" into every other unbound chat (captest #5). The
owner's decision: such a rule is keyed to its chat. A chat bound to a project, or to a folder it named, keeps
today's project or folder keying. The rules are saved and taken back through the real owner turn reader
(observe_turn) and read back through the prompt's own standing_block.
"""
from __future__ import annotations

import pytest

from core import standing_instructions as si
from core.operator_profile_turn import profile_frame_for_result

DESKTOP = "/Users/owner/Desktop"
RULE = "Always end your answers with OK-TEST."
TAKE_BACK = "Stop ending your answers with OK-TEST."


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_store_path", lambda: tmp_path / "standing_instructions.json")


def _ctx(chat: str, binding: str, *, folder: str = DESKTOP, project: str = "") -> dict:
    ctx = {"surface": "chat", "_owner_local": True, "workspace_root": folder, "workspace": folder,
           "workspace_binding": binding, "chat_id": chat}
    if project:
        ctx["_trusted_project_id"] = project
    return ctx


def _say(text: str, ctx: dict) -> dict:
    return si.observe_turn(text, ctx, principal="owner_local", session_id=ctx["chat_id"])


def _sees(ctx: dict) -> bool:
    return RULE in si.standing_block(ctx, principal="owner_local")


def test_a_rule_in_a_default_folder_chat_is_keyed_to_that_chat_and_a_new_chat_does_not_get_it():
    here = _ctx("chat-a", "default")
    assert si.workspace_key(here) == "chat:chat-a"
    assert _say(RULE, here)["saved"] == [RULE]
    assert _sees(here)
    # A new unbound chat on the same Desktop does not get it ...
    assert not _sees(_ctx("chat-b", "default"))
    # ... and neither does a chat that named the Desktop as its folder.
    assert not _sees(_ctx("chat-c", "explicit"))
    assert [item.workspace for item in si.list_active()] == ["chat:chat-a"]


def test_the_take_back_in_the_same_chat_removes_it_and_one_in_another_chat_does_not():
    here = _ctx("chat-a", "default")
    _say(RULE, here)
    elsewhere = _say(TAKE_BACK, _ctx("chat-b", "default"))
    assert elsewhere["taken_back"] == []
    assert _sees(here)
    assert _say(TAKE_BACK, here)["taken_back"] == [RULE]
    assert not _sees(here)


def test_a_turn_placed_in_a_marked_code_folder_by_an_unbound_chat_is_still_that_chat():
    from core.windows_marked_code_folder import BINDING

    marked = _ctx("chat-a", BINDING, folder="/Users/owner/Code")
    assert si.workspace_key(marked) == "chat:chat-a"
    _say(RULE, marked)
    assert _sees(_ctx("chat-a", "default"))
    assert not _sees(_ctx("chat-b", BINDING, folder="/Users/owner/Code"))


def test_a_project_chat_keeps_the_project_key_and_every_chat_in_the_project_gets_it():
    first = _ctx("chat-p1", "project", folder="/srv/projects/atlas", project="proj-atlas")
    assert si.workspace_key(first) == "project:proj-atlas"
    _say(RULE, first)
    assert _sees(_ctx("chat-p2", "project", folder="/srv/projects/atlas", project="proj-atlas"))
    assert not _sees(_ctx("chat-p3", "default"))


def test_an_explicit_folder_chat_keeps_the_folder_key_and_every_chat_on_that_folder_gets_it(tmp_path):
    folder = str(tmp_path / "atlas")
    first = _ctx("chat-f1", "explicit", folder=folder)
    assert si.workspace_key(first).startswith("folder:")
    _say(RULE, first)
    assert _sees(_ctx("chat-f2", "explicit", folder=folder))
    assert not _sees(_ctx("chat-f3", "default", folder=folder))


def test_a_context_without_a_lane_binding_or_without_a_chat_keeps_the_folder_key():
    no_binding = {"surface": "chat", "_owner_local": True, "workspace_root": DESKTOP, "chat_id": "chat-a"}
    assert si.workspace_key(no_binding).startswith("folder:")
    no_chat = _ctx("", "default")
    assert si.workspace_key(no_chat).startswith("folder:")


@pytest.mark.parametrize(("applies_to", "where"), [
    ("chat", "Saved for this chat: "),
    ("project", "Saved for every chat in this project: "),
    ("folder", "Saved for every chat in this folder: "),
    ("default", "Saved for every chat: "),
])
def test_the_save_confirmation_says_where_the_rule_applies(applies_to, where):
    obs = {"standing_instructions": {"saved": [RULE], "taken_back": [], "applies_to": applies_to}}
    frame = profile_frame_for_result({"source_context": {"profile_observation": obs}})
    assert frame and [item["report"] for item in frame["saved"]] == [where + RULE]
