"""The pet's bubble calls an untitled chat "New chat", never a piece of its session id.

Owner hand test, Windows 11: the detached pet's bubble said "c17aeaa6" for a chat with no title.
The pet takes its title line from the chat page's ``chatTitleFor``, which falls back to the last
eight characters of the session id (fine as a history label, not as words for a person). The pet
keeps the chat page as the title's owner, but an id tail is not a title.

Runs the REAL ``chatTitleFor`` from the chat page and the REAL pet functions from the fragment in
node, so it needs no browser and no Windows.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from core.companion_presentation_fragment import render_companion_fragment
from core.vool_chat_page import render_vool_chat_html

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(source: str, name: str) -> str:
    match = re.search(r"^function " + re.escape(name) + r"\(.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, f"{name} not found"
    return match.group(0)


def _bound_title(sessions: list[dict], chat_id: str, catalog: dict | None = None) -> str:
    page = render_vool_chat_html()
    frag = render_companion_fragment()
    script = (
        "let _lastSessions = " + json.dumps(sessions) + ";\n"
        + ("const __cat = " + json.dumps(catalog) + "; function VOOLT(k){ return __cat[k] || k; }\n" if catalog else "")
        + _function(page, "chatTitleFor")
        + _function(frag, "vnLabel")
        + _function(frag, "vnBoundChatTitle")
        + "process.stdout.write(JSON.stringify(vnBoundChatTitle(" + json.dumps(chat_id) + ")));\n"
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


SID = "sess-7f3e2b9a-4c1d-4e8b-a2f0-91d4c17aeaa6"


def test_an_untitled_chat_is_new_chat_not_its_id_tail() -> None:
    title = _bound_title([{"session_id": SID, "title": ""}], SID)
    assert title == "New chat"
    assert "c17aeaa6" not in title


def test_a_chat_the_page_has_not_listed_yet_is_new_chat() -> None:
    assert _bound_title([], SID) == "New chat"


def test_a_titled_chat_keeps_its_title() -> None:
    assert _bound_title([{"session_id": SID, "title": "Market research"}], SID) == "Market research"


def test_a_whitespace_title_is_untitled() -> None:
    assert _bound_title([{"session_id": SID, "title": "   "}], SID) == "New chat"


def test_new_chat_goes_through_the_page_catalog_when_it_has_one() -> None:
    title = _bound_title([{"session_id": SID, "title": ""}], SID, {"companion.new_chat": "Neuer Chat"})
    assert title == "Neuer Chat"


def test_an_unbound_pet_still_has_no_title() -> None:
    assert _bound_title([{"session_id": SID, "title": ""}], "") == ""


def test_the_fragment_never_falls_back_to_the_id_tail() -> None:
    body = _function(render_companion_fragment(), "vnBoundChatTitle")
    assert not re.search(r"return\s+id\.slice\(-8\)", body), body
