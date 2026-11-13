"""Consistency rows 2 (composer half), 3 and 8 from the Steve claims sweep.

Row 3: a chat's mode (Manual, Review edits, Auto, Plan) is kept with the chat on the server and comes
back when the chat opens, after a restart too (the desktop window does not keep its browser storage).
A new chat keeps the mode you were in.
Row 8: the sidebar list asked the memory authority again for every transcript row, opening the memory
store twice each time (25-34 s with 101 chats on Steve). One request now reads it once per chat under
one held fence, and a forgotten word is still withheld.
Row 2: "/" in the composer lists the commands that work, from the same table /help answers from.

The page checks run in a real browser (tests/served_browser.launch_chromium: skipped without
Chromium, required in the gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import runtime_paths
from core.context_namespace import ensure_chat_namespace
from core.memory import entries
from core.persistent_memory import append_conversation_event, list_conversation_sessions, set_session_meta
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get, dispatch_post

_A = "openclaw:" + "a" * 20
_B = "openclaw:" + "b" * 20


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "home" / "workspace"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _chat(sid: str, *turns: str) -> None:
    ensure_chat_namespace(sid, grant_current_receipts=False)
    policy = entries.resolve_memory_access_policy(chat_id=sid)
    for text in turns or ("hello",):
        append_conversation_event(session_id=sid, user_input=text, assistant_output="ok", access_policy=policy)


def _row(sid: str) -> dict:
    return next(row for row in list_conversation_sessions(limit=1000) if row["session_id"] == sid)


def _post(path: str, body: dict):
    return dispatch_post(path=path, body=body, headers={"content-type": "application/json"},
                         runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                         workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1")


def _status_and_body(response) -> tuple[int, dict]:
    return int(response.status), json.loads(response.body.decode("utf-8"))


# --- row 3: the mode is kept with the chat ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["manual", "review_edits", "auto", "plan"])
def test_a_chat_keeps_its_mode_on_the_server(mode) -> None:
    _chat(_A)
    assert entries._set_session_mode(_A, mode) is True
    assert _row(_A)["mode"] == mode
    # A restart reads it again from the store on disk, not from anything the page kept.
    runtime_paths.configure_runtime_home(runtime_paths.active_vool_home())
    assert _row(_A)["mode"] == mode


def test_a_chat_with_no_mode_kept_lists_none_and_another_chat_is_not_touched() -> None:
    _chat(_A)
    _chat(_B)
    entries._set_session_mode(_A, "plan")
    assert _row(_B)["mode"] == ""
    assert _row(_A)["mode"] == "plan"


@pytest.mark.parametrize("bad", ["bypass_permissions", "", "everything", "PLAN; drop"])
def test_bypass_and_unknown_modes_are_refused_and_nothing_is_written(bad) -> None:
    _chat(_A)
    with pytest.raises(ValueError):
        entries._set_session_mode(_A, bad)
    assert _row(_A)["mode"] == ""


def test_a_mode_never_makes_a_chat_that_does_not_exist() -> None:
    assert entries._set_session_mode("openclaw:" + "z" * 20, "plan") is False
    assert all(row["session_id"] != "openclaw:" + "z" * 20 for row in list_conversation_sessions(limit=1000))


def test_a_mode_keeps_the_chats_title_and_project() -> None:
    _chat(_A)
    set_session_meta(_A, title="Kept title", emoji="🌱")
    entries._set_session_mode(_A, "auto")
    row = _row(_A)
    assert (row["title"], row["emoji"], row["mode"]) == ("Kept title", "🌱", "auto")


def test_the_chat_session_endpoint_stores_a_mode() -> None:
    _chat(_A)
    status, body = _status_and_body(_post("/api/chat/session", {"session_id": _A, "mode": "Plan"}))
    assert status == 200 and body == {"session_id": _A, "mode": "plan", "stored": True}
    assert _row(_A)["mode"] == "plan"


@pytest.mark.parametrize("body", [
    {"session_id": _A, "mode": "bypass_permissions"},
    {"session_id": _A, "mode": "root"},
    {"session_id": _A, "mode": "plan", "title": "x"},
    {"session_id": _A, "mode": "plan", "delete": True},
])
def test_the_chat_session_endpoint_refuses_a_bad_mode_request(body) -> None:
    _chat(_A)
    status, _ = _status_and_body(_post("/api/chat/session", body))
    assert status == 400
    row = _row(_A)
    assert row["mode"] == "" and row["title"] != "x"


def test_the_sessions_endpoint_hands_the_mode_back() -> None:
    _chat(_A)
    entries._set_session_mode(_A, "review_edits")
    response = dispatch_get(path="/api/chat/sessions", query={}, runtime=RuntimeServices(display_name="VOOL"),
                            model_name="vool")
    status, body = _status_and_body(response)
    assert status == 200
    assert next(row for row in body["sessions"] if row["session_id"] == _A)["mode"] == "review_edits"


# --- row 8: one reading of the authority per list ------------------------------------------------------


def test_the_list_opens_the_memory_store_a_fixed_number_of_times(monkeypatch) -> None:
    from core import context_retrieval as cr

    for index in range(6):
        _chat(f"openclaw:chat{index:02d}" + "x" * 12, "one", "two", "three")
    opened = []
    real = cr._open_memory

    def counting(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(cr, "_open_memory", counting)
    rows = list_conversation_sessions(limit=1000)
    assert len(rows) == 6
    # Before: two opens for each of the 18 transcript rows and again for each listed chat (>= 48).
    assert len(opened) <= 2, len(opened)


def test_the_list_still_withholds_what_the_authority_withholds(monkeypatch) -> None:
    from core.memory import revocation

    _chat(_A, "The lantern marker is MINT-84.")
    _chat(_B, "Plain words.")
    real = revocation._one_reading_tokens

    def with_a_forget(state, chat_id):
        tokens = real(state, chat_id)
        return (*tokens, "mint-84") if chat_id == _A else tokens

    monkeypatch.setattr(revocation, "_one_reading_tokens", with_a_forget)
    rows = list_conversation_sessions(limit=1000)
    assert "MINT-84" not in json.dumps(rows)
    assert any(row["session_id"] == _B for row in rows)


def test_an_unreadable_authority_still_withholds_everything(monkeypatch) -> None:
    from core import context_retrieval as cr

    _chat(_A, "The lantern marker is MINT-84.")
    monkeypatch.setattr(cr, "_open_memory", lambda *a, **k: None)
    assert "MINT-84" not in json.dumps(list_conversation_sessions(limit=1000))


def test_a_failed_read_for_one_chat_is_not_remembered_as_empty(monkeypatch) -> None:
    from core.memory import revocation

    _chat(_A, "first", "second")
    calls = []
    real = revocation.snapshot

    def flaky(chat_id=None, runtime_home=None):
        calls.append(chat_id)
        if len(calls) == 1:
            raise RuntimeError("memory erasure is incomplete")
        return real(chat_id, runtime_home)

    monkeypatch.setattr(revocation, "snapshot", flaky)
    with revocation._one_reading():
        assert revocation.authorize_payload({"session_id": _A, "user": "first"}, chat_id=_A) is False
        assert revocation.authorize_payload({"session_id": _A, "user": "first"}, chat_id=_A) is True


def test_the_reading_ends_with_the_list() -> None:
    from core.memory import revocation

    _chat(_A)
    list_conversation_sessions(limit=10)
    assert getattr(revocation._ONE_READING, "state", None) is None


# --- row 2: one table of working commands ------------------------------------------------------------


def test_the_commands_endpoint_serves_the_help_table() -> None:
    from core.slash_commands import WORKING_SLASH_COMMANDS

    response = dispatch_get(path="/api/chat/commands", query={}, runtime=RuntimeServices(display_name="VOOL"),
                            model_name="vool")
    status, body = _status_and_body(response)
    assert status == 200
    assert [(c["command"], c["description"]) for c in body["commands"]] == list(WORKING_SLASH_COMMANDS)
    assert "/help" in {c["command"] for c in body["commands"]}


# --- the page, in a real browser ------------------------------------------------------------------------

_STUB = """
window.__urls = []; window.__posts = [];
const json = (o) => new Response(JSON.stringify(o), {headers: {'Content-Type': 'application/json'}});
window.__sessions = [
  {session_id: 'chat-a', title: 'Alpha planning chat', mode: 'plan', turn_count: 2},
  {session_id: 'chat-b', title: 'Beta release checklist', mode: '', turn_count: 1}];
window.fetch = async (url, opts) => {
  url = String(url); window.__urls.push(url);
  if (opts && opts.method === 'POST') window.__posts.push([url, opts.body ? JSON.parse(opts.body) : null]);
  if (url.includes('/api/chat/sessions')) return json({sessions: window.__sessions});
  if (url.includes('/api/chat/history')) return json({messages: []});
  if (url.includes('/api/chat/commands')) return json({commands: [
    {command: '/help', description: 'List the commands that work in this chat.'},
    {command: '/new', description: 'Show where to start a new chat.'},
    {command: '/stop', description: 'Stop the reply that is running.'},
    {command: '/stopall', description: 'Stop all of VOOL.'},
    {command: '/trace', description: 'Get the link to the live trace rail.'}]});
  if (url.includes('/api/chat/session') && opts && opts.method === 'POST') {
    const b = JSON.parse(opts.body || '{}');
    const rec = window.__sessions.find((s) => s.session_id === b.session_id);
    if (!rec) return json({session_id: b.session_id, mode: b.mode, stored: false});
    rec.mode = b.mode;
    return json({session_id: b.session_id, mode: b.mode, stored: true});
  }
  return json({ok: true});
};
"""


def _page(tmp_path: Path):
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
    page.wait_for_timeout(1000)
    return manager, browser, page, errors


def _mode_label(page) -> str:
    return page.locator("#modeLbl").inner_text().strip()


def test_a_chat_opens_in_the_mode_the_server_kept_and_a_new_chat_keeps_it(tmp_path) -> None:
    manager, browser, page, errors = _page(tmp_path)
    try:
        # A fresh window (nothing in its storage, as after a restart) opens a chat kept in Plan.
        page.locator("#sessions .session", has_text="Alpha planning chat").first.click()
        page.wait_for_timeout(500)
        assert _mode_label(page) == "Plan"
        page.locator("#sessions .session", has_text="Beta release checklist").first.click()
        page.wait_for_timeout(500)
        assert _mode_label(page) == "Manual"
        # Choosing a mode hands it to the server with the chat.
        page.evaluate("window.__posts = []")
        page.locator("#modeBtn").click()
        page.locator('#modePop [data-mode="auto"]').click()
        page.wait_for_timeout(300)
        assert any(u.endswith("/api/chat/session") and b == {"session_id": "chat-b", "mode": "auto"}
                   for u, b in page.evaluate("window.__posts"))
        # New chat keeps the mode you were in, and asks the server to make nothing yet.
        page.evaluate("window.__posts = []")
        page.evaluate("newChat()")
        page.wait_for_timeout(300)
        assert _mode_label(page) == "Auto"
        assert not any(u.endswith("/api/chat/session") for u, _ in page.evaluate("window.__posts"))
        # Back on Alpha: still Plan.
        page.locator("#sessions .session", has_text="Alpha planning chat").first.click()
        page.wait_for_timeout(500)
        assert _mode_label(page) == "Plan"
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_slash_lists_the_working_commands_and_tab_completes(tmp_path) -> None:
    manager, browser, page, errors = _page(tmp_path)
    try:
        box = page.locator("#input")
        box.click()
        box.type("/")
        page.wait_for_timeout(300)
        menu = page.locator("#slashMenu")
        assert menu.is_visible()
        names = page.locator("#slashMenu .slash-item b").all_inner_texts()
        assert names == ["/help", "/new", "/stop", "/stopall", "/trace"]
        box.type("st")
        page.wait_for_timeout(200)
        assert page.locator("#slashMenu .slash-item b").all_inner_texts() == ["/stop", "/stopall"]
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Tab")
        assert box.input_value() == "/stopall"
        assert not menu.is_visible()
        # Escape closes it, and ordinary words never open it.
        box.fill("")
        box.type("/h")
        page.wait_for_timeout(200)
        assert menu.is_visible()
        page.keyboard.press("Escape")
        assert not menu.is_visible()
        box.fill("")
        box.type("hello /help")
        page.wait_for_timeout(200)
        assert not menu.is_visible()
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_enter_on_a_command_typed_in_full_sends_it(tmp_path) -> None:
    manager, browser, page, errors = _page(tmp_path)
    try:
        page.evaluate("() => { window.__sent = []; window.send = async () => { window.__sent.push(inputEl.value); }; }")
        box = page.locator("#input")
        box.click()
        box.type("/he")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")   # completes, does not send
        assert box.input_value() == "/help"
        assert page.evaluate("window.__sent") == []
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__sent") == ["/help"]
        assert page.locator("#slashMenu").is_hidden()
        assert errors == []
    finally:
        browser.close()
        manager.stop()
