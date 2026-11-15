"""Consistency row 2: the Windows chat's slash commands say what the window really has.

Steve's Windows VOOL window (consistency report, item 2): /new, /clear and /reset answered "Use the
OpenClaw `New session` button on the lower right", but the window has no OpenClaw and its button is
"+ New" at the top left; /help said "not wired here"; and /stop was not wired although the footer
says "Esc stop".

Pinned here:

- On Windows (the write gate's `_IS_WINDOWS`), /new, /clear and /reset point to "+ New" at the top
  left with no OpenClaw text, and /help lists only commands the Windows chat answers. Each listed
  command is driven through the served /api/chat the window posts to, with no model round.
- Mac and Linux keep their replies byte for byte.
- In the chat page (one page on every host), a typed `/stop` presses the running task's Stop
  control -- the control Esc presses -- and is never posted. Proven in a real browser
  (tests/served_browser.launch_chromium: skipped without chromium outside the gate).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.agent_runtime.fast_paths_utility import ui_command_fast_path
from tests.test_windows_run_in_folder_served_end_to_end import chat, jobs, machine

_NEW_CHAT = ("/new", "/clear", "/reset")


def _reply(command: str) -> str:
    return str(ui_command_fast_path(command, source_surface="api") or "")


# --- the lane itself ------------------------------------------------------------------------------


@pytest.mark.parametrize("command", _NEW_CHAT)
def test_windows_new_chat_commands_point_to_plus_new_at_the_top_left(monkeypatch, command) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    reply = _reply(command)
    assert "+ New" in reply and "top left" in reply, reply
    assert "OpenClaw" not in reply and "lower right" not in reply, reply
    # The words the base tests read ("New session") stay; only the place and the button change.
    assert "New session" in reply, reply


def test_windows_help_lists_the_shared_table_and_no_openclaw(monkeypatch) -> None:
    from core.slash_commands import WORKING_SLASH_COMMANDS

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    reply = _reply("/help")
    assert "not wired" not in reply and "OpenClaw" not in reply, reply
    assert re.findall(r"^- `(/[a-z]+)`", reply, flags=re.MULTILINE) == [c for c, _ in WORKING_SLASH_COMMANDS]
    for command, description in WORKING_SLASH_COMMANDS:
        assert f"- `{command}` {description}" in reply
    assert [c for c, _ in WORKING_SLASH_COMMANDS] == ["/help", "/new", "/stop", "/stopall", "/trace"]
    assert "+ New" in reply and "top left" in reply, reply


def test_the_table_is_plain_one_line_text() -> None:
    from core.slash_commands import WORKING_SLASH_COMMANDS

    for command, description in WORKING_SLASH_COMMANDS:
        assert re.fullmatch(r"/[a-z]+", command), command
        assert description and "\n" not in description and "`" not in description and "**" not in description


def test_windows_help_names_the_real_stopall_confirm_window(monkeypatch) -> None:
    from core import vool_agent_brake

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert f"/stopall confirm within {int(vool_agent_brake._STOPALL_CONFIRM_SECONDS)} seconds" in _reply("/help")


def test_windows_unknown_command_points_to_help_not_to_a_missing_button(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    reply = _reply("/whatever")
    assert "/help" in reply and "New session" not in reply and "OpenClaw" not in reply, reply


def test_an_absolute_path_is_still_not_a_command_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert ui_command_fast_path("/etc/hosts what is in this file", source_surface="api") is None


def test_mac_and_linux_replies_are_byte_identical(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    new_session = (
        "New session: use the `+ New` button in the chat sidebar. Slash `/new` is not a wired command in this runtime."
    )
    other = (
        "That slash command is not wired here. Use plain language, the `New session` button, or open "
        "`http://127.0.0.1:11435/trace` for the runtime rail."
    )
    for command in _NEW_CHAT:
        assert _reply(command) == new_session
    assert _reply("/trace") == "Open the live trace rail at `http://127.0.0.1:11435/trace`."
    assert _reply("/help") == other
    assert _reply("/stop") == other


# --- through the served /api/chat the VOOL window posts to ---------------------------------------


def _say(open_chat, text: str) -> str:
    return open_chat.say(text, turn_id=f"turn-{uuid.uuid4().hex[:10]}")


def test_served_windows_chat_answers_new_clear_reset_and_help(machine, chat) -> None:
    conversation = chat(machine["desktop"])
    for command in _NEW_CHAT:
        reply = _say(conversation, command)
        assert "+ New" in reply and "top left" in reply and "OpenClaw" not in reply, (command, reply)
    help_reply = _say(conversation, "/help")
    assert "/stopall confirm" in help_reply and "not wired" not in help_reply, help_reply
    assert conversation.model_rounds == 0


def test_served_windows_chat_runs_every_command_help_lists(machine, chat, monkeypatch) -> None:
    from core import vool_agent_brake

    shutdowns: list[str] = []
    monkeypatch.setattr(vool_agent_brake, "_default_stopall", lambda: shutdowns.append("stop") or True)
    # The brake reads the host from os.name; on a real Windows host it and the write gate agree.
    monkeypatch.setattr(vool_agent_brake, "_is_windows", lambda: True)
    conversation = chat(machine["desktop"])

    trace = _say(conversation, "/trace")
    assert "http://127.0.0.1:11435/trace" in trace, trace

    # /stopall asks first and stops nothing; /stopall confirm in the same chat runs it once.
    asked = _say(conversation, "/stopall")
    assert "/stopall confirm" in asked and shutdowns == [], asked
    confirmed = _say(conversation, "/stopall confirm")
    assert "Shutting down VOOL now" in confirmed and "OpenClaw" not in confirmed, confirmed
    assert shutdowns == ["stop"]
    again = _say(conversation, "/stopall confirm")
    assert "nothing was stopped" in again and shutdowns == ["stop"], again

    # /help and /new are answered above; /stop from a surface other than the window (which takes it
    # itself, below) is answered too, not sent to the model.
    stop = _say(conversation, "/stop")
    assert "Esc" in stop and "not wired" not in stop, stop
    assert conversation.model_rounds == 0


# --- the chat page: a typed /stop does what Esc does ---------------------------------------------

_STUB = """
window.__posts = [];
const json = (o) => new Response(JSON.stringify(o), {headers: {'Content-Type': 'application/json'}});
window.fetch = async (url, opts) => {
  url = String(url);
  if (opts && String(opts.method || '').toUpperCase() === 'POST') window.__posts.push(url);
  if (url.includes('/api/chat/sessions')) return json({sessions: []});
  if (url.includes('/api/chat/history')) return json({messages: []});
  return json({ok: true});
};
"""

_LIVE_TASK = """
window.__stops = 0;
const card = document.createElement('div');
card.className = 'task-card';
const stop = document.createElement('button');
stop.className = 'tc-stop'; stop.textContent = 'Stop';
stop.addEventListener('click', () => { window.__stops++; stop.style.display = 'none'; });
card.appendChild(stop);
document.getElementById('log').appendChild(card);
"""


def _open_page(tmp_path: Path):
    from core.vool_chat_page import render_vool_chat_html
    from tests.served_browser import launch_chromium

    path = tmp_path / "chat.html"
    path.write_text(render_vool_chat_html(), encoding="utf-8")
    manager, browser = launch_chromium()
    page = browser.new_page(viewport={"width": 1100, "height": 760})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(_STUB)
    page.goto(path.as_uri())
    page.wait_for_timeout(800)
    return manager, browser, page, errors


def _chat_posts(page) -> list[str]:
    return [u for u in page.evaluate("window.__posts") if "/api/chat" in u and "/sessions" not in u]


@pytest.mark.parametrize("windows", [True, False])
def test_typed_stop_presses_the_running_task_stop_like_esc(tmp_path, monkeypatch, windows) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    manager, browser, page, errors = _open_page(tmp_path)
    try:
        page.evaluate(_LIVE_TASK)
        page.fill("#input", "/stop")
        page.press("#input", "Enter")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__stops") == 1
        assert page.input_value("#input") == ""
        assert "Stopping" in page.text_content("#nToast")
        assert _chat_posts(page) == [], "a typed /stop is never sent"

        # Esc presses the very same control.
        page.evaluate(_LIVE_TASK)
        page.focus("#input")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__stops") == 1  # the fresh card's counter
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_typed_stop_with_nothing_running_says_so_and_sends_nothing(tmp_path) -> None:
    manager, browser, page, errors = _open_page(tmp_path)
    try:
        page.fill("#input", "/stop")
        page.press("#input", "Enter")
        page.wait_for_timeout(200)
        assert page.text_content("#nToast") == "Nothing is running in this chat."
        assert page.input_value("#input") == ""
        assert _chat_posts(page) == []
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_stop_with_more_words_is_still_sent(tmp_path) -> None:
    """Only the bare command is the typed Esc: "/stop x402" is the spend brake and goes to VOOL."""

    manager, browser, page, errors = _open_page(tmp_path)
    try:
        page.evaluate(_LIVE_TASK)
        page.fill("#input", "/stop x402")
        page.press("#input", "Enter")
        page.wait_for_timeout(400)
        assert page.evaluate("window.__stops") == 0
        assert errors == []
    finally:
        browser.close()
        manager.stop()
