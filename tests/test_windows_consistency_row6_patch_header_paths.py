"""Consistency row 6, real Windows: a patch whose diff adds a file is not refused for its `/dev/null` header.

Steve (bundled Python 3.12.7, integrate daaefe7b): "apply this patch:" with a diff over calc.py and a new
notes.txt was answered "`/dev/null` is not a full local path. ... Nothing was written." before any card. The
front door's typed-path check (`write_root_honesty.non_local_write_target`, under `windows_path_policy`) read
the new file's `--- /dev/null` header as a write destination: the word "add" in calc.py's context line
(`def add(a, b):`) stood before it as a write verb. A diff with only the new file has no such word, so it
passed. The diff under a patch request's first line is the patch's text; the patch tool judges its paths.

Linux runs this with both Windows seams forced: the write gate's `_IS_WINDOWS` (the patch lane) and
`windows_path_policy._IS_WINDOWS` (the typed-path check), which the fixtures leave off.
"""
from __future__ import annotations

import os
import uuid

import pytest

from core import windows_path_policy
from core.agent_runtime.write_root_honesty import non_local_write_target
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return b + a\n"
NEW_FILE = "--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1 @@\n+hello\n"


@pytest.fixture
def paths_on_windows(machine, monkeypatch):
    """The path policy on, as on Windows. The fixtures' folders are POSIX temp folders, which the policy
    would refuse as the chat's own workspace (no drive letter); on Windows they are local drive folders, so
    the workspace checks are told so and every other check of the policy runs as written."""
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "chat_workspace_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(windows_path_policy, "context_workspace_refusal", lambda *_a, **_k: None)
    return machine


def test_a_two_file_patch_with_a_new_file_gets_its_card_and_applies(paths_on_windows, chat, jobs) -> None:
    proj = paths_on_windows["proj"]
    (proj / "calc.py").write_bytes(CALC.encode("utf-8"))
    talk = chat(proj)

    card, reply = talk.ask_allow_resume("apply this patch:\n" + DIFF + NEW_FILE)

    assert "/dev/null" not in reply, reply
    assert card["intent"] == "workspace.apply_unified_diff", card
    assert not any("/dev/null" in item for item in card["affected_resources"]), card
    assert os.path.samefile(card["affected_resources"][0], proj / "calc.py"), card
    assert talk.model_rounds == 0, reply
    assert (proj / "calc.py").read_text(encoding="utf-8") == CALC.replace("return a + b", "return b + a")
    assert (proj / "notes.txt").read_text(encoding="utf-8").strip() == "hello"


def test_the_typed_path_check_reads_only_the_request_line_of_a_patch(paths_on_windows) -> None:
    assert non_local_write_target("apply this patch:\n" + DIFF + NEW_FILE) == ("", "")
    # The request line is still read: a share it names to write to is refused as before.
    share = non_local_write_target("apply this patch and write it to \\\\host\\share\\x.txt\n" + DIFF)
    assert share[1], share
    # A message that is not a patch request is read whole, as before.
    assert non_local_write_target("add a line to /dev/null")[1]


def test_off_windows_the_check_is_unchanged(machine, monkeypatch) -> None:
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)
    assert non_local_write_target("apply this patch:\n" + DIFF + NEW_FILE) == ("", "")
