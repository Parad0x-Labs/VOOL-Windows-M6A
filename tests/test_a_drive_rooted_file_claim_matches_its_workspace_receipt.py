"""A truthful file claim spelled with a Windows drive root keeps its receipt's backing.

The honesty check maps a workspace-relative receipt path (``reports/a.md``) under the turn's
workspace root before comparing it with a qualified claim. A drive-rooted workspace
(``D:\\ws``) is absolute on Windows, so the claim ``I wrote a.md (at D:\\ws\\reports\\a.md).``
names exactly the file the receipt recorded and must be kept; a claim about another folder
must still be refused. The strings are Windows spellings on every OS, so this runs anywhere.
"""

from __future__ import annotations

import uuid

import pytest

from core.agent_runtime.action_honesty_validator import enforce_final_action_honesty

_RECEIPT = [{"tool_name": "file.deliver", "status": "executed",
             "execution": {"executed": True, "ok": True},
             "observation": {"path": "reports/a.md", "format": "md"}}]


def _kept(root: str, location: str) -> bool:
    text = f"I wrote a.md (at {location})."
    result = enforce_final_action_honesty({"response": text}, user_input="Save the file", session_id=uuid.uuid4().hex,
                                          source_context={"workspace_root": root, "tool_receipts": _RECEIPT})
    return result["response"] == text


@pytest.mark.parametrize("root,location", [
    ("D:\\a\\_temp\\ws", "D:\\a\\_temp\\ws\\reports\\a.md"),
    ("D:\\a\\_temp\\ws", "D:/a/_temp/ws/reports/a.md"),
    ("D:\\a\\_temp\\ws", "d:\\a\\_temp\\ws\\reports\\a.md"),
    ("C:\\Users\\me\\ws", "C:\\Users\\me\\ws\\reports\\a.md"),
])
def test_the_receipted_file_under_a_drive_root_is_kept(root, location):
    assert _kept(root, location)


@pytest.mark.parametrize("root,location", [
    ("D:\\a\\_temp\\ws", "D:\\a\\_temp\\ws\\other\\a.md"),
    ("D:\\a\\_temp\\ws", "E:\\a\\_temp\\ws\\reports\\a.md"),
    ("D:\\a\\_temp\\ws", "D:\\a\\_temp\\elsewhere\\reports\\a.md"),
])
def test_a_drive_rooted_claim_about_another_place_is_refused(root, location):
    assert not _kept(root, location)


def test_the_posix_spelling_is_unchanged():
    assert _kept("/tmp/ws", "/tmp/ws/reports/a.md")
    assert not _kept("/tmp/ws", "/tmp/ws/other/a.md")
