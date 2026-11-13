"""In the desktop window the per-answer PDF is saved only through the native save bridge.

pywebview cancels page downloads unless ALLOW_DOWNLOADS is set, and VOOL never sets it. The answer's
PDF button fell back to an ``a.download`` click whenever the bridge lacked ``save_answer_pdf``: in the
desktop window that click is cancelled, yet the button said "Downloaded". The chat bundle and the chat
export already refuse in that case and say to update the app; the answer PDF now does the same. A
plain browser (no ``window.pywebview``) keeps its download.
"""
from __future__ import annotations

import json

from tests.chat_page_js_harness import run_node, script, slice_source

_FN = slice_source(script(), "function mountAnswerExport(", "async function mountProofChip(")

_PROGRAM = """
const clicks = [];
function node(tag) {
  return {
    tag, children: [], textContent: '', title: '', className: '', disabled: false, listeners: {},
    set download(v) { this._download = v; }, get download() { return this._download; },
    setAttribute() {}, appendChild(c) { this.children.push(c); return c; }, remove() {},
    querySelector(sel) { return sel === '.msg-actions' ? this.actions : null; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    click() { clicks.push(this._download || ''); },
  };
}
globalThis.document = { createElement: node, body: { appendChild() {} } };
globalThis.URL = { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} };
globalThis.fetch = async () => ({ ok: true, blob: async () => ({ type: 'application/pdf' }) });
globalThis.setTimeout = () => 0;
async function run(pywebview) {
  clicks.length = 0;
  if (pywebview === undefined) delete globalThis.window; else globalThis.window = { pywebview };
  if (globalThis.window === undefined) globalThis.window = {};
  const host = node('div'); host.actions = node('div');
  mountAnswerExport(host, 'chat-1', 'answer-1');
  const button = host.actions.children[0], status = host.children[0];
  await button.listeners.click();
  return { downloads: clicks.slice(), status: status.textContent };
}
%s
const out = {
  desktop_without_bridge: await run({ api: {} }),
  desktop_with_bridge: await run({ api: { save_answer_pdf: async () => ({ ok: true }) } }),
  browser: await run(undefined),
};
console.log(JSON.stringify(out));
"""


def _run() -> dict:
    return run_node(_PROGRAM % _FN.replace("mountAnswerReport", "undefinedReport"))


def test_desktop_window_without_the_bridge_method_refuses_instead_of_a_cancelled_download() -> None:
    out = _run()
    case = out["desktop_without_bridge"]
    assert case["downloads"] == [], json.dumps(case)
    assert "Update the app" in case["status"], case
    assert case["status"] != "Downloaded"


def test_desktop_window_saves_through_the_bridge_and_the_browser_keeps_its_download() -> None:
    out = _run()
    assert out["desktop_with_bridge"] == {"downloads": [], "status": "Saved"}
    assert out["browser"] == {"downloads": ["vool-answer.pdf"], "status": "Downloaded"}
