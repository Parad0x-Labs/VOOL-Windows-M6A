"""B11 and B15 on the chat page.

B11: the project 📂 button ignored /api/projects/reveal's answer, so a failed reveal did nothing.
B15: a file dropped outside the message box made the window navigate to the file.
Both handlers are cut out of the served page and run under node with stand-in browser objects.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from core.vool_chat_page import render_vool_chat_html

NODE = shutil.which("node")


def _page() -> str:
    return render_vool_chat_html()


def _block(html: str, start: str, end: str) -> str:
    begin = html.index(start)
    return html[begin:html.index(end, begin)]


def _node(script: str) -> dict:
    if NODE is None:
        pytest.fail("node is required for the chat page handler tests")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


_DROP_HARNESS = """
const listeners = {};
const toasts = [];
const document = { addEventListener(type, fn) { listeners[type] = fn; } };
function toast(t) { toasts.push(t); }
%s
function fire(type, types, defaultPrevented) {
  const e = { defaultPrevented: defaultPrevented, prevented: false,
              dataTransfer: { types: types, dropEffect: 'copy' },
              preventDefault() { this.prevented = true; } };
  listeners[type](e);
  return { prevented: e.prevented, dropEffect: e.dataTransfer.dropEffect };
}
const result = {
  outsideOver: fire('dragover', ['Files'], false),
  outsideDrop: fire('drop', ['Files'], false),
  footerOver: fire('dragover', ['Files'], true),
  footerDrop: fire('drop', ['Files'], true),
  textDrop: fire('drop', ['text/plain'], false),
  toasts: toasts,
};
console.log(JSON.stringify(result));
"""


def test_a_file_dropped_outside_the_message_box_does_not_navigate() -> None:
    block = _block(_page(), "// A file dropped anywhere else in the window", "if (inputEl && typeof inputEl.addEventListener")
    r = _node(_DROP_HARNESS % block)
    assert r["outsideOver"] == {"prevented": True, "dropEffect": "none"}
    assert r["outsideDrop"]["prevented"] is True
    assert r["toasts"] == ["Drop files on the message box to attach them."]


def test_drops_on_the_message_box_and_text_drops_are_left_alone() -> None:
    block = _block(_page(), "// A file dropped anywhere else in the window", "if (inputEl && typeof inputEl.addEventListener")
    r = _node(_DROP_HARNESS % block)
    # The footer handler ran first (defaultPrevented): the document handler changes nothing.
    assert r["footerOver"] == {"prevented": False, "dropEffect": "copy"}
    assert r["footerDrop"]["prevented"] is False
    assert r["textDrop"]["prevented"] is False


def test_the_footer_still_takes_file_drops_before_the_document_guard() -> None:
    html = _page()
    assert html.index("composerFooterEl.addEventListener('drop'") < html.index("document.addEventListener('drop'")


_REVEAL_HARNESS = """
const toasts = [];
function toast(t) { toasts.push(t); }
const _serverProjects = { p1: { name: 'App', root: %s } };
let reply = null;
async function fetch(url, opts) {
  if (reply instanceof Error) throw reply;
  return { ok: reply.status === 200, json: async () => reply.body };
}
%s
(async () => {
  const out = {};
  for (const [name, r] of Object.entries({
      opened: { status: 200, body: { ok: true, id: 'p1' } },
      refused: { status: 200, body: { ok: false, id: 'p1' } },
      missing: { status: 404, body: { error: 'project folder not found' } },
      offline: new Error('offline') })) {
    toasts.length = 0; reply = r;
    await revealProject('p1');
    out[name] = toasts.slice();
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.parametrize("root,comma", [("C:\\\\Users\\\\x\\\\My App", False), ("C:\\\\Users\\\\x\\\\Smith, J", True)])
def test_a_failed_project_reveal_says_so(root: str, comma: bool) -> None:
    fn = _block(_page(), "async function revealProject(pid) {", "async function assignSession(")
    r = _node(_REVEAL_HARNESS % (json.dumps(root), fn))
    assert r["opened"] == []
    for case in ("refused", "missing", "offline"):
        assert len(r[case]) == 1 and r[case][0].startswith("Could not show this project")
        assert ("contains a comma" in r[case][0]) is comma
