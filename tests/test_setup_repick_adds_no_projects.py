"""Re-picking a folder in Setup adds no project; only "Use this folder" saves one.

Hardware: each pick in Setup's folder step created a NEW project (3 picks, 3 extra projects in the
sidebar), because "Choose a folder…" saved the pick at once. Replacing an earlier pick would need
/api/projects/delete, which also unbinds that project's chats and forgets their memory, and the
page cannot tell a project Setup created from one that existed (create is an upsert by path). So a
pick now only fills the box and says so; the one explicit "Use this folder" saves it.

The REAL controlFolder runs in node over the DOM stand-in of
tests/test_setup_folder_step_says_what_happened.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from core.vool_setup_page import render_vool_setup_html
from tests.test_setup_folder_step_says_what_happened import _DOM, FOLDER, NOTE, _function

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PICKS = ["C:\\Users\\me\\One", "C:\\Users\\me\\Two", "C:\\Users\\me\\Three"]


def _run(steps: str) -> dict:
    html = render_vool_setup_html()
    fns = "".join(_function(html, n) for n in ("renderControl", "fail", "controlFolder", "setFolderResult",
                                                       "clearFailNote", "notSaved", "draft", "hasFolderPicker"))
    rig = (_DOM.replace("__STEP__", json.dumps(FOLDER)).replace("__AFTER__", json.dumps({**FOLDER, "note": NOTE}))
           .replace("__POST__", json.dumps({"ok": True, "status": 200, "json": {"project": {"id": "p"}}}))
           .replace("__MARK__", "null"))
    rig = rig.replace("async function pickFolder() { return null; }",
                      "const PICKS = " + json.dumps(PICKS) + "; async function pickFolder() { return PICKS.shift() || null; }")
    # 0.7.2 offers Choose only where a picker exists: framed in the chat, the host has one.
    rig = rig.replace("const framed = () => false;", "const framed = () => true;")
    script = rig + fns + "(async () => { renderControl(STEP);\n" + steps + """
  const res = byId('folderResult');
  console.log(JSON.stringify({ posts: out.posts, value: byId('folderPath').value, result: res ? res.textContent : null }));
})().catch((e) => console.log(JSON.stringify({ error: String(e && e.stack || e) })));"""
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert "error" not in result, result["error"]
    return result


def test_three_picks_save_nothing() -> None:
    out = _run("for (let i = 0; i < 3; i++) await byId('chooseFolder').fire('click');")
    assert out["posts"] == []
    assert out["value"] == PICKS[-1]
    assert "Use this folder" in out["result"]


def test_use_saves_exactly_the_last_pick_once() -> None:
    out = _run("for (let i = 0; i < 3; i++) await byId('chooseFolder').fire('click');\n"
               "await byId('useFolder').fire('click');")
    assert out["posts"] == [["/api/projects", {"name": "Three", "root": PICKS[-1]}]]
