"""Setup's folder step keeps the path, says what happened, and offers the Windows mark.

Owner hand test, Windows 11: typed C:\\Users\\...\\Documents\\vool-test (a real plain folder) and
clicked "Use this folder". POST /api/projects answered 200, then refresh() re-rendered the step,
which emptied the box; on Windows the step stays not-done until the folder is marked disposable,
so the only feedback was the off-screen live region's "Not confirmed yet." Another POST answered
400 with nothing visible.

The honest outcome is NOT to count an unmarked folder as done (VOOL cannot change files there):
the step keeps the path, shows the result as visible text (the server's own words on a refusal),
and on Windows offers "Mark this folder so VOOL can change files…", which asks the native bridge's
mark_disposable_folder with the Setup window as the dialog's owner. The bridge still takes no path
from the page: the folder comes from the OS picker and the consent from the OS confirm.

The REAL controlFolder runs in node over a tiny DOM stand-in.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from core.vool_setup_page import render_vool_setup_html

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

NOTE = ("Picked. On Windows VOOL can change files there only after you mark it as disposable "
        "in Settings, then restart VOOL.")


def _function(source: str, name: str) -> str:
    match = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, f"{name} not found"
    return match.group(0)


_DOM = r"""
const ids = {};
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.value = ''; this.listeners = {}; this.dataset = {}; this.attrs = {}; this.hidden = false; this.disabled = false; this.className = ''; this._id = ''; }
  set id(v) { this._id = v; ids[v] = this; } get id() { return this._id; }
  set textContent(v) { this._text = String(v); this.children = []; } get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  async fire(t, e) { for (const f of this.listeners[t] || []) await f(e || { key: '', preventDefault() {} }); }
  setAttribute(k, v) { this.attrs[k] = v; } focus() {}
  attached() { let n = this; while (n.parent) n = n.parent; return n === HOST_ROOT; }
}
const HOST_ROOT = new Node('body');
const document = { createElement: (t) => new Node(t), querySelector: (s) => { const n = ids[s.slice(1)]; return n && n.attached() ? n : null; } };
const control = new Node('div'); control.id = 'control'; HOST_ROOT.appendChild(control);
const liveNode = new Node('div'); liveNode.id = 'live'; HOST_ROOT.appendChild(liveNode);
const view = { snap: null, index: 1, motion: 'auto' };
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const live = (msg) => { liveNode.textContent = msg; };
function T(key, fallback) { return fallback; }
const fmt = (s, vars) => String(s).replace(/\{(\w+)\}/g, (_, k) => (vars[k] == null ? '' : vars[k]));
const out = { posts: [], marks: [] };
let STEP = __STEP__;
const AFTER_SAVE = __AFTER__;
const POST = __POST__;
const MARK = __MARK__;
function current() { return STEP; }
async function postJSON(url, body) { out.posts.push([url, body]); return POST; }
async function refresh() { if (AFTER_SAVE) STEP = AFTER_SAVE; renderControl(STEP); return {}; }
async function pickFolder() { return null; }
const window = { pywebview: MARK === null ? undefined : { api: { mark_disposable_folder: async (owner) => { out.marks.push(owner === undefined ? null : owner); return MARK; } } } };
function find(pred, n) { n = n || control; if (pred(n)) return n; for (const c of n.children) { const f = find(pred, c); if (f) return f; } return null; }
const byId = (id) => find((n) => n.id === id);
"""


def _run(step: dict, steps: str, after: dict | None = None, post: dict | None = None, mark=None) -> dict:
    html = render_vool_setup_html()
    fns = "".join(_function(html, name) for name in ("renderControl", "fail", "controlFolder", "setFolderResult"))
    rig = (_DOM.replace("__STEP__", json.dumps(step)).replace("__AFTER__", json.dumps(after))
           .replace("__POST__", json.dumps(post or {"ok": True, "status": 200, "json": {"project": {"id": "p1"}}}))
           .replace("__MARK__", json.dumps(mark)))
    script = rig + fns + "(async () => { renderControl(STEP);\n" + steps + "\n})().catch((e) => { console.log(JSON.stringify({error: String(e && e.stack || e)})); });"
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert "error" not in result, result["error"]
    return result


FOLDER = {"id": "folder", "done": False, "skipped": False, "later": "the + Project button"}
PATH = "C:\\Users\\me\\Documents\\vool-test"
REPORT = """
  const res = byId('folderResult');
  const mk = byId('markFolder');
  console.log(JSON.stringify({ value: byId('folderPath').value, result: res ? res.textContent : null,
    cls: res ? res.className : null, hidden: res ? res.hidden : null, mark: mk ? mk.textContent : null,
    markHidden: mk ? mk.hidden : null, posts: out.posts, marks: out.marks, live: liveNode.textContent }));
"""
USE = f"""
  const box = byId('folderPath'); box.value = {json.dumps(PATH)}; await box.fire('input');
  await byId('useFolder').fire('click');
"""


def test_the_typed_path_survives_the_re_render_after_saving() -> None:
    out = _run(FOLDER, USE + REPORT, after={**FOLDER, "skipped": True, "note": NOTE})
    assert out["posts"] == [["/api/projects", {"name": "vool-test", "root": PATH}]]
    assert out["value"] == PATH, "the box was emptied by the re-render"


def test_a_refusal_shows_the_servers_words_visibly() -> None:
    post = {"ok": False, "status": 400, "json": {"error": "that folder does not exist"}}
    out = _run(FOLDER, USE + REPORT, post=post)
    assert out["result"] and "that folder does not exist" in out["result"]
    assert "failed" in out["cls"] and out["hidden"] is False
    assert out["value"] == PATH


def test_an_http_failure_without_a_body_still_says_so() -> None:
    out = _run(FOLDER, USE + REPORT, post={"ok": False, "status": 500, "json": None})
    assert out["result"] and "HTTP 500" in out["result"] and "failed" in out["cls"]


def test_saved_on_windows_says_so_and_offers_the_mark() -> None:
    out = _run(FOLDER, USE + REPORT, after={**FOLDER, "note": NOTE}, mark={"ok": False, "cancelled": True})
    assert out["result"] and "vool-test" in out["result"] and "mark" in out["result"].lower()
    assert "failed" not in out["cls"] and out["hidden"] is False
    assert out["mark"] == "Mark this folder so VOOL can change files…" and out["markHidden"] is False
    assert out["live"], "the screen reader hears it too"


def test_the_mark_asks_the_bridge_with_the_setup_window_as_owner() -> None:
    steps = USE + "await byId('markFolder').fire('click');" + REPORT
    out = _run(FOLDER, steps, after={**FOLDER, "note": NOTE}, mark={"ok": True, "id": "x", "pending_restart": True})
    assert out["marks"] == ["setup"], "the page names its window and passes no path"
    assert "Restart VOOL" in out["result"] and "done" in out["cls"]


def test_a_refused_mark_shows_why() -> None:
    steps = USE + "await byId('markFolder').fire('click');" + REPORT
    out = _run(FOLDER, steps, after={**FOLDER, "note": NOTE},
               mark={"ok": False, "refused": "This folder can't be marked: it is your whole user folder."})
    assert "whole user folder" in out["result"] and "failed" in out["cls"]


def test_a_cancelled_mark_keeps_the_saved_message() -> None:
    steps = USE + "await byId('markFolder').fire('click');" + REPORT
    out = _run(FOLDER, steps, after={**FOLDER, "note": NOTE}, mark={"ok": False, "cancelled": True})
    assert "vool-test" in out["result"] and "failed" not in out["cls"]


def test_no_mark_offer_where_the_folder_is_already_usable() -> None:
    out = _run(FOLDER, USE + REPORT, after={**FOLDER, "done": True}, mark={"ok": True})
    assert out["result"] == "Saved: VOOL works inside vool-test." and "done" in out["cls"]
    assert out["mark"] is None or out["markHidden"] is True


def test_no_mark_offer_without_the_native_bridge() -> None:
    out = _run(FOLDER, USE + REPORT, after={**FOLDER, "note": NOTE}, mark=None)
    assert out["mark"] is None or out["markHidden"] is True
    assert "Settings" in out["result"], "without the bridge it says where to mark it"


def test_an_unmarked_folder_is_never_counted_done(monkeypatch) -> None:
    from core import setup_progress, windows_write_gate

    source = _function(render_vool_setup_html(), "controlFolder")
    assert "done:true" not in source.replace(" ", "").lower()
    monkeypatch.setattr(windows_write_gate, "active", lambda: True)
    monkeypatch.setattr(setup_progress, "_picked_folders", lambda: [PATH])
    monkeypatch.setattr(setup_progress, "_windows_writable", lambda _folder: False)
    assert setup_progress.folder_done() is False
    assert setup_progress.folder_unmarked() is True
