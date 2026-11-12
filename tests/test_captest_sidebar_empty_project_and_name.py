"""Two sidebar defects from the Windows captest fresh install.

1. Expanding a project with no chats showed nothing: the caret flipped and the group stayed
   blank, which read as a broken drop-down. An empty, expanded project now says "No chats yet."
   and offers "New chat here" (the same action as the head's + button).
2. "+ Project" suggested the folder's name by splitting the path on "/" only, so on Windows the
   whole path ("C:\\Users\\...") became the project's name. It now takes the base name on either
   separator, the way the Setup folder step already does. Stored names are left alone.

Read from the served page text; host-independent.
"""
from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOGS = ROOT / "core" / "i18n" / "catalogs"


def _page() -> str:
    from core.vool_chat_page import render_vool_chat_html

    return render_vool_chat_html()


def _function(page: str, header: str) -> str:
    start = page.index(header)
    end = page.index("\n}\n", start)
    return page[start:end]


def _project_loop(page: str) -> str:
    body = _function(page, "function renderSessions(sessions)")
    start = body.index("for (const pid of projectIds)")
    end = body.index("if (projectIds.length)", start)
    return body[start:end]


def test_an_expanded_empty_project_says_it_has_no_chats():
    loop = _project_loop(_page())
    assert "items.length" in loop, "the empty case is not distinguished"
    assert "pageT('chats.project_empty', 'No chats yet.')" in loop
    assert "side-empty" in loop, "the empty line reuses the sidebar's empty style"


def test_the_empty_line_offers_a_new_chat_in_that_project():
    loop = _project_loop(_page())
    assert "pageT('chats.project_new_chat', 'New chat here')" in loop
    assert re.search(r"newChatInProject\(pid\)", loop), "the action must open a chat in THIS project"
    assert "stopPropagation" in loop, "clicking the action must not also collapse the group"


def test_a_collapsed_project_still_shows_nothing_under_its_head():
    loop = _project_loop(_page())
    assert re.search(r"if \(!isCollapsed\)", loop), "the empty line belongs to the expanded state only"


def test_every_catalog_has_the_empty_project_strings():
    files = sorted(CATALOGS.glob("*.json"))
    assert files
    for path in files:
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        for key in ("chats.project_empty", "chats.project_new_chat"):
            assert str(messages.get(key) or "").strip(), f"{path.name} lacks {key}"


def _suggestion_expression(page: str) -> str:
    body = _function(page, "async function createProjectFlow(assignSid)")
    found = re.search(r"const suggested = (root\.split\(.*?\)\.filter\(Boolean\)\.pop\(\) \|\| 'Project');", body)
    assert found, "createProjectFlow no longer derives a suggested name"
    return found.group(1)


def test_new_project_suggests_the_folder_base_name_on_either_separator():
    expression = _suggestion_expression(_page())
    assert expression.startswith("root.split(/[\\\\/]/)"), expression
    # The same split, applied the way the page applies it.
    for path, expected in (
        (r"C:\Users\me\Documents\ASUS", "ASUS"),
        ("C:\\Users\\me\\Projects\\Council\\", "Council"),
        ("/Users/me/code/extensions", "extensions"),
        ("C:/Users/me/Telegram", "Telegram"),
    ):
        parts = [p for p in re.split(r"[\\/]", path) if p]
        assert (parts[-1] if parts else "Project") == expected


def test_new_project_matches_the_setup_folder_step():
    from core.vool_setup_page import render_vool_setup_html

    setup = render_vool_setup_html()
    assert "clean.split(/[\\\\/]/).filter(Boolean).pop() || 'Project'" in setup
    assert "root.split('/')" not in _function(_page(), "async function createProjectFlow(assignSid)")


def test_a_stored_project_name_is_shown_as_stored():
    """Existing names are not rewritten or re-derived for display."""
    head = _function(_page(), "function makeProjectHead(pid, name, count, collapsed, lcState)")
    assert "nameEl.textContent = (proj.root && proj.exists === false) ? (name + ' ⚠') : name;" in head
