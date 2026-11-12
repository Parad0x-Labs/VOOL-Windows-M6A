"""A chat's mode is kept with the chat the moment it is picked, not after the mode controller answers.

Steve round 22 (bbc15ea4): the page stored a chat's mode only after POST /api/mode returned, which took 3 to 23 s
there (Plan took 11.9 s to stick). Closing VOOL before that reopened the chat in Manual: Plan showed on the button,
but the mode kept with the chat was "". Now the pick goes to POST /api/chat/session at once; if the controller
then refuses the mode, the chat's previous mode is put back.

Real browser (tests/served_browser.launch_chromium: skipped without Chromium, required in the gate).
"""
from __future__ import annotations

from pathlib import Path

_STUB = """
window.__posts = [];
window.__modeAnswer = null;            // the test answers POST /api/mode when it chooses
const json = (o, status) => new Response(JSON.stringify(o), {status: status || 200,
  headers: {'Content-Type': 'application/json'}});
window.__sessions = [
  {session_id: 'chat-a', title: 'Alpha planning chat', mode: 'auto', turn_count: 2},
  {session_id: 'chat-b', title: 'Beta release checklist', mode: '', turn_count: 1}];
window.fetch = async (url, opts) => {
  url = String(url);
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (opts && opts.method === 'POST') window.__posts.push([url.replace(/^.*(\\/api\\/)/, '/api/'), body]);
  if (url.includes('/api/chat/sessions')) return json({sessions: window.__sessions});
  if (url.includes('/api/chat/history')) return json({messages: []});
  if (url.endsWith('/api/chat/session') && body && body.mode) {
    const rec = window.__sessions.find((s) => s.session_id === body.session_id);
    if (rec) rec.mode = body.mode;
    return json({session_id: body.session_id, mode: body.mode, stored: !!rec});
  }
  if (url.endsWith('/api/mode') && body && body.op === 'set') {
    // Slow, as on Steve: it answers only when the test says so.
    while (!window.__modeAnswer) await new Promise((r) => setTimeout(r, 50));
    const answer = window.__modeAnswer;
    return answer.ok ? json({state: {mode: body.mode}}) : json({error: answer.error}, 400);
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


def _kept(page) -> list:
    return [body for url, body in page.evaluate("window.__posts")
            if url == "/api/chat/session" and body and body.get("mode")]


def _pick(page, mode: str) -> None:
    page.locator("#modeBtn").click()
    page.locator(f'#modePop [data-mode="{mode}"]').click()


def test_a_picked_mode_is_kept_with_the_chat_before_the_controller_answers(tmp_path) -> None:
    manager, browser, page, errors = _page(tmp_path)
    try:
        page.locator("#sessions .session", has_text="Beta release checklist").first.click()
        page.wait_for_timeout(400)
        page.evaluate("window.__posts = []")
        _pick(page, "plan")
        page.wait_for_timeout(300)
        # POST /api/mode is still waiting; the chat already holds Plan (a restart now keeps it).
        assert page.evaluate("window.__modeAnswer") is None
        assert {"session_id": "chat-b", "mode": "plan"} in _kept(page)
        assert page.evaluate("window.__sessions.find((s) => s.session_id === 'chat-b').mode") == "plan"
        page.evaluate("window.__modeAnswer = {ok: true}")
        page.wait_for_timeout(300)
        assert page.locator("#modeLbl").inner_text().strip() == "Plan"
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_a_mode_the_controller_refuses_puts_the_previous_mode_back(tmp_path) -> None:
    manager, browser, page, errors = _page(tmp_path)
    try:
        page.locator("#sessions .session", has_text="Alpha planning chat").first.click()
        page.wait_for_timeout(400)
        assert page.locator("#modeLbl").inner_text().strip() == "Auto"
        page.evaluate("window.__posts = []")
        _pick(page, "plan")
        page.wait_for_timeout(300)
        assert {"session_id": "chat-a", "mode": "plan"} in _kept(page)
        page.evaluate("window.__modeAnswer = {ok: false, error: 'refused'}")
        page.wait_for_timeout(400)
        assert _kept(page)[-1] == {"session_id": "chat-a", "mode": "auto"}
        assert page.evaluate("window.__sessions.find((s) => s.session_id === 'chat-a').mode") == "auto"
        assert errors == []
    finally:
        browser.close()
        manager.stop()
