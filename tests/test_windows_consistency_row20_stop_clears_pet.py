"""Consistency round 20: after a typed /stop the pet says the turn was cancelled, the same as after Esc.

Steve's Windows VOOL window: a bare /stop while a reply ran stopped the run, but the pet's bubble
stayed on its last working phrase ("Thinking · Drawing the route…").

Cause: the pet hears a turn's end only from the server's typed `task.cancelled` frame on the
/api/chat stream. Stop -- pressed, Esc, or a typed /stop, which all press the same `.tc-stop`
control -- aborts that stream in the same click, so the frame can never arrive, and
`applyTaskEvent` ignores everything once the run has ended. `finishRun` now reports the end it
shows on the card (cancelled or failed) to the pet when no typed terminal reached it.

Driven in a real browser against the chat page (one page on every host), with a stubbed
/api/chat stream that is still working when Stop lands and dies on abort, as a real fetch does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import windows_write_gate as gate

_STUB = r"""
window.__posts = [];
const json = (o) => new Response(JSON.stringify(o), {headers: {'Content-Type': 'application/json'}});
window.fetch = async (url, opts) => {
  url = String(url);
  const post = opts && String(opts.method || '').toUpperCase() === 'POST';
  if (post) window.__posts.push(url);
  if (url.includes('/api/chat/commands')) return json({commands: [
    {command: '/help'}, {command: '/new'}, {command: '/stop'}, {command: '/stopall'}, {command: '/trace'}]});
  if (url.includes('/api/chat/sessions')) return json({sessions: []});
  if (url.includes('/api/chat/history')) return json({messages: []});
  if (url.includes('/api/cloud/model')) return json({ok: true, model: '', provider: ''});
  if (post && /\/api\/chat$/.test(url)) {
    const signal = opts.signal;
    const enc = new TextEncoder();
    const body = new ReadableStream({start(c) {
      const frame = (ev) => c.enqueue(enc.encode(JSON.stringify({vool_event: ev}) + '\n'));
      frame({type: 'task.started', seq: 1});
      frame({type: 'agent.plan', seq: 2, stage: 'Planning', summary: 'Planning the steps'});
      window.__dropStream = () => c.error(new TypeError('network error'));
      if (signal) signal.addEventListener('abort', () => {
        try { c.error(new DOMException('The user aborted a request.', 'AbortError')); } catch (e) {}
      });
    }});
    return new Response(body, {headers: {'Content-Type': 'application/x-ndjson'}});
  }
  return json({ok: true});
};
"""

_PET = """() => {
  const view = window.VoolCompanion.view(displayedChat);
  const res = window.VoolCompanion.resolve(view);
  const bubble = document.querySelector('#companionLayer .vn-bubble');
  return {terminal: view.terminal, state: res.state, phrase: res.phraseText, tone: res.tone,
          bubble: bubble ? bubble.textContent : null,
          stopVisible: !!window.VoolPageActions.hasLiveRun()};
}"""


def _open_page(tmp_path: Path):
    from core.vool_chat_page import render_vool_chat_html
    from tests.served_browser import launch_chromium

    tmp_path.mkdir(parents=True, exist_ok=True)
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


def _start_reply(page) -> dict:
    page.fill("#input", "plan my week")
    page.press("#input", "Enter")
    page.wait_for_timeout(600)
    running = page.evaluate(_PET)
    assert running["stopVisible"], running
    assert running["terminal"] is None and running["state"] == "MIDDLE", running
    return running


def _pet_after(tmp_path: Path, how: str) -> dict:
    manager, browser, page, errors = _open_page(tmp_path)
    try:
        _start_reply(page)
        if how == "esc":
            page.focus("#input")
            page.keyboard.press("Escape")
        else:
            page.fill("#input", "/stop")
            page.wait_for_timeout(150)  # the "/" list has opened on /stop and /stopall
            page.press("#input", "Enter")
        page.wait_for_timeout(1500)
        pet = page.evaluate(_PET)
        assert "/api/chat/cancel" in page.evaluate("window.__posts"), how
        assert [u for u in page.evaluate("window.__posts") if u.endswith("/api/chat")] == ["/api/chat"], (
            "a typed /stop is never sent"
        )
        assert errors == [], errors
        return pet
    finally:
        browser.close()
        manager.stop()


@pytest.mark.parametrize("windows", [True, False])
def test_typed_stop_leaves_the_pet_exactly_as_esc_does(tmp_path, monkeypatch, windows) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    after_esc = _pet_after(tmp_path / "esc", "esc")
    after_stop = _pet_after(tmp_path / "stop", "stop")
    assert after_stop == after_esc
    # ...and that shared state is the terminal cancelled one, not the last working phrase.
    assert after_stop["terminal"] == "CANCELLED", after_stop
    assert after_stop["state"] == "CANCELLED" and after_stop["tone"] == "cancelled", after_stop
    assert after_stop["phrase"] == "Cancelled.", after_stop
    assert after_stop["bubble"] and after_stop["bubble"].endswith("Cancelled."), after_stop
    assert not after_stop["stopVisible"], after_stop


def test_a_dropped_connection_does_not_leave_the_pet_working(tmp_path) -> None:
    """The same gap without Stop: a stream that dies with no terminal frame ends the turn as failed."""

    manager, browser, page, errors = _open_page(tmp_path)
    try:
        _start_reply(page)
        page.evaluate("window.__dropStream()")
        page.wait_for_timeout(600)
        pet = page.evaluate(_PET)
        assert pet["terminal"] == "FAILED" and pet["state"] == "FAILED", pet
        assert not pet["stopVisible"], pet
        assert errors == []
    finally:
        browser.close()
        manager.stop()


def test_a_server_cancelled_frame_is_not_told_to_the_pet_twice(tmp_path) -> None:
    """When the typed task.cancelled does arrive, the page adds nothing of its own."""

    manager, browser, page, errors = _open_page(tmp_path)
    try:
        page.evaluate("""() => {
          const orig = window.VoolCompanion.consume;
          window.__petEvents = [];
          window.VoolCompanion.consume = (chatId, ev) => { window.__petEvents.push(ev.type); return orig(chatId, ev); };
        }""")
        _start_reply(page)
        page.evaluate("""() => {
          const run = chatState(displayedChat).run;
          applyTaskEvent(run, {type: 'task.cancelled', seq: 3, summary: 'Cancelled on the machine'});
        }""")
        page.wait_for_timeout(300)
        events = page.evaluate("window.__petEvents")
        assert events.count("task.cancelled") == 1 and "task.failed" not in events, events
        assert page.evaluate(_PET)["terminal"] == "CANCELLED"
        assert errors == []
    finally:
        browser.close()
        manager.stop()
