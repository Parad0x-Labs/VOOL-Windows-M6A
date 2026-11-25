"""Windows: the chat page's periodic reads share two connections, so Send and the brake never wait.

Steve, 0.7.3 (24 Nov, r073-final7 unfreeze-timeout trace): WebView2 opens at most 6 connections to
127.0.0.1:11435. The page's periodic reads (update chip every 3.5 s, reminder history every 5 s,
the ledger, the sidebar) stacked while the server was slow and, with the turn streams, took all
six; /freeze, /unfreeze and Send then waited in the browser until their 5 s timer fired. A Windows
page now runs those reads in a lane of at most two, and the exact brake slash commands skip the
model pre-read (the server answers them before any model or spend). Other hosts get the page byte
for byte.

The lane script runs under node against a fake fetch that holds every request open until told.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess

import pytest

from core import vool_chat_page as page

_HARNESS = r"""
const pending = [];
let inFlight = 0, peak = 0;
const sent = [];
globalThis.location = { href: 'http://127.0.0.1:11435/', origin: 'http://127.0.0.1:11435' };
globalThis.window = globalThis;
window.fetch = (input, init) => {
  const url = String(input);
  sent.push(url);
  inFlight++; peak = Math.max(peak, inFlight);
  return new Promise((resolve, reject) => {
    const done = () => { inFlight--; resolve(new Response(JSON.stringify({ url }), { status: 200 })); };
    pending.push({ url, done });
    if (init && init.signal) init.signal.addEventListener('abort', () => {
      const at = pending.findIndex((p) => p.done === done);
      if (at >= 0) { pending.splice(at, 1); inFlight--; reject(new Error('abort')); }
    });
  });
};
const warnings = [];
console.warn = (...args) => warnings.push(args.join(' '));
__LANES__
const tick = () => new Promise((r) => setTimeout(r, 0));
__SCENARIO__
"""

_MAIN = r"""
(async () => {
  const out = {};
  const polls = [];
  for (let i = 0; i < 6; i++) polls.push(fetch('/api/update/status'));
  polls.push(fetch('/api/chat/history?session=a&event_kind=reminder_delivery'));
  polls.push(fetch('/api/runtime/events?session=a&after=0&limit=200'));
  polls.push(fetch('/api/chat/sessions'));
  await tick();
  out.background_in_flight = inFlight;
  // The user's actions go straight out while every background slot is held.
  const sendTurn = fetch('/api/chat', { method: 'POST', body: '{}' });
  const preRead = fetch('/api/cloud/model?session_id=a');
  const opening = fetch('/api/chat/history?session=a');
  await tick();
  out.after_user_actions = inFlight;
  out.user_sent = sent.filter((u) => /\/api\/chat$|cloud\/model|history\?session=a$/.test(u)).length;
  // Release everything, in order, until the lane drains.
  while (pending.length) { pending.shift().done(); await tick(); await tick(); }
  const bodies = await Promise.all(polls.map((p) => p.then((r) => r.json())));
  out.poll_answers = bodies.length;
  out.every_poll_answered = bodies.every((b) => b && b.url);
  out.update_status_sent = sent.filter((u) => u.endsWith('/api/update/status')).length;
  out.peak_background = peak;
  await sendTurn; await preRead; await opening;
  // A queued read with its own signal leaves the lane when aborted, and never reaches the server.
  const held = [fetch('/api/runtime/version'), fetch('/api/connections?probe=0')];
  await tick();
  const ctl = new AbortController();
  const queued = fetch('/api/runtime/receipts?session=a', { signal: ctl.signal });
  ctl.abort();
  out.aborted = await queued.then(() => 'answered', (e) => e.name || String(e));
  while (pending.length) { pending.shift().done(); await tick(); await tick(); }
  await Promise.all(held);
  out.receipts_sent = sent.filter((u) => u.includes('/api/runtime/receipts')).length;
  out.brake = ['/freeze', ' /UNFREEZE ', '/stopx402', '/startx402', '/stopall'].map((t) => voolBrakeSlash(t));
  out.not_brake = ['freeze the wallet please', '/freezer', 'can you /freeze', '/freeze now', ''].map((t) => voolBrakeSlash(t));
  console.log(JSON.stringify(out));
})();
"""

_HUNG = r"""
(async () => {
  const out = {};
  // Two reads that never answer hold both slots; at the cap (shortened here) they give the slots
  // back, and the read waiting behind them goes out.
  const hung = [fetch('/api/chat/sessions'), fetch('/api/runtime/sessions?summary=1')];
  const after = fetch('/api/runtime/usage');
  await tick();
  out.before_cap = sent.filter((u) => u.endsWith('/api/runtime/usage')).length;
  out.hung = await Promise.all(hung.map((p) => p.then(() => 'answered', () => 'gave the slot back')));
  await tick();
  out.after_cap = sent.filter((u) => u.endsWith('/api/runtime/usage')).length;
  pending.filter((p) => p.url.endsWith('/api/runtime/usage')).forEach((p) => p.done());
  out.after_answered = await after.then((r) => r.ok);
  out.cap_warned = warnings.some((w) => w.includes('gave its slot back'));
  console.log(JSON.stringify(out));
})();
"""


def _run(scenario: str = _MAIN, *, cap_ms: int | None = None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to execute the page script")
    lanes = page._WINDOWS_FETCH_LANES_JS
    if cap_ms is not None:
        lanes = lanes.replace("LANE_CAP_MS = 10000", f"LANE_CAP_MS = {cap_ms}")
        assert lanes != page._WINDOWS_FETCH_LANES_JS
    program = _HARNESS.replace("__LANES__", lanes + page._WINDOWS_BRAKE_SLASH_JS).replace("__SCENARIO__", scenario)
    done = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_background_reads_hold_at_most_two_connections_and_user_actions_never_wait() -> None:
    out = _run()
    assert out["background_in_flight"] == 2
    assert out["after_user_actions"] == 5           # 2 background + Send + pre-read + opening a chat
    assert out["user_sent"] == 3
    assert out["peak_background"] <= 5


def test_every_read_is_answered_and_identical_waiting_reads_share_one_request() -> None:
    out = _run()
    assert out["poll_answers"] == 9 and out["every_poll_answered"] is True
    # Six update-chip reads at once: two go out, the four still waiting share one request (a read
    # already in flight is never shared, since it started before the later callers asked).
    assert out["update_status_sent"] == 3


def test_a_hung_background_read_gives_its_slot_back() -> None:
    out = _run(_HUNG, cap_ms=50)
    assert out["before_cap"] == 0
    assert out["hung"] == ["gave the slot back"] * 2
    assert out["after_cap"] == 1 and out["after_answered"] is True
    assert out["cap_warned"] is True


def test_an_aborted_waiting_read_leaves_the_lane_and_never_reaches_the_server() -> None:
    out = _run()
    assert out["aborted"] == "AbortError"
    assert out["receipts_sent"] == 0


def test_only_the_exact_brake_slash_commands_skip_the_pre_read() -> None:
    out = _run()
    assert out["brake"] == [True] * 5
    assert out["not_brake"] == [False] * 5


def test_each_brake_slash_command_is_a_brake_on_the_server() -> None:
    from core.vool_agent_brake import is_brake_command

    for text in ("/freeze", "/unfreeze", "/stopx402", "/startx402", "/stopall"):
        assert is_brake_command(text), text


def test_a_windows_page_carries_the_lane_and_the_brake_send(monkeypatch) -> None:
    from core import generated_files

    for mac, _ in page._WINDOWS_PAGE_LANES:
        assert page._VOOL_CHAT_HTML.count(mac) == 1, mac
    monkeypatch.setattr(generated_files, "host_is_windows", lambda: True)
    served = page._host_chat_template()
    assert "voolFetchLanes" in served
    assert "if (!voolBrakeSlash(lastUser.content || '')) try {" in served
    house = served[served.index("<script>\n"):]
    lanes_end = house.index("window.__voolFetchLanes = true;")
    # The lane wraps fetch before any other page code runs, so a read made while the page loads
    # (or a fragment's own reference to fetch) goes through it too.
    assert house.index("voolFetchLanes") < lanes_end < house.index("fetch(", lanes_end)
    assert "EventSource" not in served and "XMLHttpRequest" not in served   # nothing bypasses the wrapper


def test_off_windows_the_page_is_unchanged(monkeypatch) -> None:
    from core import generated_files

    monkeypatch.setattr(generated_files, "host_is_windows", lambda: False)
    served = page._host_chat_template()
    assert "voolFetchLanes" not in served and "voolBrakeSlash" not in served


def test_a_slow_request_writes_one_warning_line_on_windows(monkeypatch, caplog) -> None:
    from starlette.testclient import TestClient

    from core.web.api import app as api_app
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import json_response

    monkeypatch.setattr(api_app, "_SLOW_REQUEST_LOG", True)
    monkeypatch.setattr(api_app, "_SLOW_REQUEST_MS", 0.0)

    application = api_app.create_api_app(runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                                         get_dispatcher=lambda **kwargs: json_response(200, {"ok": True}))
    with caplog.at_level(logging.WARNING, logger="vool.api.http"), TestClient(application) as client:
        client.get("/api/update/status?token=secret-value", headers={"host": "127.0.0.1:11435"})
    lines = [r.getMessage() for r in caplog.records if r.name == "vool.api.http" and r.levelno == logging.WARNING]
    assert len(lines) == 1 and "slow api request GET /api/update/status status=200" in lines[0]
    assert "secret-value" not in lines[0]


def test_off_windows_no_slow_request_line(monkeypatch, caplog) -> None:
    from starlette.testclient import TestClient

    from core.web.api import app as api_app
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import json_response

    monkeypatch.setattr(api_app, "_SLOW_REQUEST_LOG", False)
    monkeypatch.setattr(api_app, "_SLOW_REQUEST_MS", 0.0)
    application = api_app.create_api_app(runtime=RuntimeServices(display_name="VOOL"), model_name="vool",
                                         get_dispatcher=lambda **kwargs: json_response(200, {"ok": True}))
    with caplog.at_level(logging.WARNING, logger="vool.api.http"), TestClient(application) as client:
        client.get("/api/update/status", headers={"host": "127.0.0.1:11435"})
    assert not [r for r in caplog.records if r.name == "vool.api.http" and r.levelno == logging.WARNING]
