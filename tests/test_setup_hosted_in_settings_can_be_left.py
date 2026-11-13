"""Setup opened from Settings: "Do this later" always leaves it, and the folder step says one thing.

Owner hand test, Windows 11 (screenshot): Setup ran INSIDE the "VOOL Settings" window, because the
Settings page's Continue tried window.open (which the native window does not give a new window
for) and then navigated the Settings window itself to /setup. There:

(a) "Do this later" did nothing: the page asked the bridge's close_setup, which answers
    {ok:false, error:'not_open'} when the page is not the bridge's own Setup window, and stopped.
    Now a refused or failed close falls back to leaving in place (back to Settings when it came
    from there), and Settings opens Setup in its own native window when the bridge is there.
(b) The folder step showed "Picked. On Windows VOOL can change files there only after you mark
    it…" and "Skipped — you can do this later…" together. The note is the step's real state (a
    folder is picked, not usable yet); the status line no longer also says Skipped.
(c) Every VOOL window, the Settings window included, opens through the helper that applies the
    Windows chrome (see tests/test_every_vool_window_gets_windows_chrome.py).

The page JS runs in node with small stand-ins, so this needs no browser and no Windows.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import types

import pytest

from core.vool_settings_page import render_vool_settings_html
from core.vool_setup_page import render_vool_setup_html

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(source: str, name: str) -> str:
    match = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, f"{name} not found"
    return match.group(0)


def _run(script: str):
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


_CLOSE_RIG = r"""
const out = { closeSetupCalls: 0, posted: [], closed: false, href: null };
const BRIDGE = __BRIDGE__;
const REFERRER = __REFERRER__;
const FRAMED = __FRAMED__;
const window = {
  location: { origin: 'http://127.0.0.1:11435', set href(v) { out.href = v; }, get href() { return 'http://127.0.0.1:11435/setup'; } },
  get closed() { return out.closed; },
  close() { if (__CLOSE_WORKS__) out.closed = true; },
  parent: { postMessage(m) { out.posted.push(m.type); } },
  pywebview: BRIDGE === null ? undefined : { api: { close_setup() { out.closeSetupCalls++; return BRIDGE === 'throw' ? Promise.reject(new Error('x')) : Promise.resolve(BRIDGE); } } },
};
const document = { referrer: REFERRER };
const framed = () => FRAMED;
"""


def _close(bridge, referrer="", framed=False, close_works=False) -> dict:
    html = render_vool_setup_html()
    fns = "".join(_function(html, name) for name in ("closeSetup", "leaveSetupInPlace", "cameFromSettings"))
    rig = (_CLOSE_RIG.replace("__BRIDGE__", json.dumps(bridge)).replace("__REFERRER__", json.dumps(referrer))
           .replace("__FRAMED__", json.dumps(framed)).replace("__CLOSE_WORKS__", json.dumps(close_works)))
    return _run(rig + fns + "closeSetup(); setTimeout(() => console.log(JSON.stringify(out)), 20);")


@needs_node
def test_its_own_setup_window_still_closes_through_the_bridge() -> None:
    out = _close({"ok": True}, referrer="http://127.0.0.1:11435/settings")
    assert out["closeSetupCalls"] == 1 and out["href"] is None


@needs_node
def test_inside_the_settings_window_do_this_later_goes_back_to_settings() -> None:
    out = _close({"ok": False, "error": "not_open"}, referrer="http://127.0.0.1:11435/settings#setup")
    assert out["closeSetupCalls"] == 1
    assert out["href"] == "/settings", out


@needs_node
def test_a_failing_bridge_still_leaves_setup() -> None:
    out = _close("throw", referrer="http://127.0.0.1:11435/settings")
    assert out["href"] == "/settings", out


@needs_node
def test_not_from_settings_and_not_closable_falls_back_to_the_chat() -> None:
    out = _close({"ok": False, "error": "not_open"}, referrer="")
    assert out["href"] == "/chat", out
    other_origin = _close({"ok": False, "error": "not_open"}, referrer="http://evil.example/settings")
    assert other_origin["href"] == "/chat", other_origin


@needs_node
def test_a_browser_tab_that_can_close_just_closes() -> None:
    out = _close(None, referrer="", close_works=True)
    assert out["closed"] is True and out["href"] is None


@needs_node
def test_framed_setup_still_asks_its_host() -> None:
    out = _close({"ok": False}, framed=True)
    assert out["posted"] == ["vool-setup-close"] and out["closeSetupCalls"] == 0 and out["href"] is None


def test_settings_opens_setup_in_its_own_native_window() -> None:
    body = _function(render_vool_settings_html(), "openSetupPage")
    assert "window.pywebview.api.open_setup(stepId || '')" in body
    framed_branch = body.index("window.top !== window.self")
    assert framed_branch < body.index("window.pywebview.api.open_setup") < body.index("window.open(url, 'vool-setup')")


# --- (b) one status for the folder step ---------------------------------------------------------

_STATUS_RIG = r"""
const COPY = { done: 'Done — you can change this later in {later}.', skipped: 'Skipped — you can do this later in {later}.' };
const fmt = (s, vars) => String(s).replace(/\{(\w+)\}/g, (_, k) => (vars[k] == null ? '' : vars[k]));
"""


def _status(step: dict) -> list:
    fns = _function(render_vool_setup_html(), "stepStatus")
    return _run(_STATUS_RIG + fns + "console.log(JSON.stringify(stepStatus(" + json.dumps(step) + ")));")


@needs_node
def test_a_picked_unmarked_folder_is_not_also_skipped() -> None:
    later = "the “+ Project” button in the chat sidebar"
    step = {"id": "folder", "done": False, "skipped": True, "later": later,
            "note": "Picked. On Windows VOOL can change files there only after you mark it as disposable in Settings, then restart VOOL."}
    assert _status(step) == ["", ""]


@needs_node
def test_done_and_skipped_still_read_as_before() -> None:
    later = "Settings"
    assert _status({"done": True, "skipped": False, "later": later}) == [
        "done", "Done — you can change this later in Settings."]
    assert _status({"done": False, "skipped": True, "later": later}) == [
        "skipped", "Skipped — you can do this later in Settings."]
    assert _status({"done": False, "skipped": False, "later": later}) == ["", ""]


def test_the_page_renders_the_status_through_that_one_function() -> None:
    html = render_vool_setup_html()
    render = _function(html, "render")
    assert "const [stateClass, stateText] = stepStatus(step);" in render
    assert "st.textContent = stateText;" in render


# --- (c) the Settings window gets the chrome ----------------------------------------------------


def test_the_settings_window_opens_through_the_chrome_helper(monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    opened: list = []
    monkeypatch.setattr(nw, "_create_vool_window", lambda module, *a, **k: opened.append(a[0]) or types.SimpleNamespace(
        events=types.SimpleNamespace(closed=_Hook())))
    api = nw._WindowApi()
    api.set_window(object(), types.SimpleNamespace())
    assert api.open_settings()["ok"] is True
    assert api.open_setup()["ok"] is True
    assert opened == ["VOOL Settings", "VOOL Setup"]


class _Hook:
    def __iadd__(self, _handler):
        return self
