"""Consistency row 6: a patch's approval card names the files the diff changes.

Steve (Windows): the card for `workspace.apply_unified_diff` read "Affects: -" while every other card named
its path, because a patch names its targets in its `---`/`+++` headers and no `path` argument. The card now
lists each file the diff names, resolved where the patch tool writes it (the folder the patch is placed in),
and says so plainly for a name the tool would refuse.

Driven through the served chat with the fixtures of tests/test_windows_run_in_folder_served_end_to_end.py:
the card is the pending approval (`talk.pending()`), the resumed turn runs the real patch tool, and the model
is never asked.
"""
from __future__ import annotations

import os
import uuid

import pytest

from core import mode_permission_policy as mpp
from core import windows_write_gate as gate
from tests.test_windows_run_in_folder_served_end_to_end import CALC, chat, jobs, machine

DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return b + a\n"
NEW_FILE = "--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1 @@\n+hello\n"


def _turn() -> str:
    return f"turn-{uuid.uuid4().hex[:10]}"


def _same(shown: list[str], expected: list) -> bool:
    return len(shown) == len(expected) and all(os.path.samefile(a, b) for a, b in zip(shown, expected, strict=True))


ASKS = {
    "fenced": "Apply this patch:\n```diff\n" + DIFF + "```",
    "unfenced": "apply this patch\n" + DIFF,
    "folder-first": "In {proj}, apply this unified diff:\n```diff\n" + DIFF + "```",
    "to-a-file": "apply this patch to {proj}{sep}calc.py\n" + DIFF,
}


@pytest.mark.parametrize("form", list(ASKS))
def test_the_patch_card_names_the_resolved_diff_target(machine, chat, jobs, form) -> None:
    proj = machine["proj"]
    target = proj / "calc.py"
    target.write_bytes(CALC.encode("utf-8"))
    # The chat is bound to the unmarked Desktop for the forms that name their folder, so the card's path
    # can only come from where the patch is placed, not from the chat's own folder.
    talk = chat(proj if form in {"fenced", "unfenced"} else machine["desktop"])

    card, reply = talk.ask_allow_resume(ASKS[form].format(proj=proj, sep=os.sep))

    assert card["intent"] == "workspace.apply_unified_diff", card
    assert _same(card["affected_resources"], [target]), card["affected_resources"]
    assert os.path.isabs(card["affected_resources"][0])
    assert talk.model_rounds == 0, reply
    assert target.read_text(encoding="utf-8") == CALC.replace("return a + b", "return b + a"), reply


def test_a_patch_that_creates_a_file_names_the_new_file_not_dev_null(machine, chat) -> None:
    proj = machine["proj"]
    talk = chat(proj)

    talk.say("apply this patch:\n" + NEW_FILE, turn_id=_turn())

    (card,) = talk.pending()
    assert card["affected_resources"] == [str((proj / "notes.txt").resolve())], card["affected_resources"]
    assert not any("/dev/null" in item for item in card["affected_resources"])
    assert not (proj / "notes.txt").exists()


def test_a_patch_over_two_files_names_both(machine, chat) -> None:
    proj = machine["proj"]
    (proj / "calc.py").write_bytes(CALC.encode("utf-8"))
    talk = chat(proj)

    talk.say("apply this patch:\n" + DIFF + NEW_FILE, turn_id=_turn())

    (card,) = talk.pending()
    assert _same(card["affected_resources"][:1], [proj / "calc.py"]), card["affected_resources"]
    assert card["affected_resources"][1:] == [str((proj / "notes.txt").resolve())], card["affected_resources"]


def test_a_name_the_patch_tool_refuses_is_shown_as_written_and_said_so() -> None:
    """Placement is the patch tool's own (`resolve_write_target`): a name that leaves the folder is not
    turned into a path it does not write; it is shown as the diff writes it, with the reason."""
    escape = DIFF.replace("a/calc.py", "a/../outside.py").replace("b/calc.py", "b/../outside.py")
    shown = mpp._patch_targets_display({"patch": escape + NEW_FILE}, {"workspace": "/nonexistent/ws"})

    assert shown[0] == "../outside.py (as the patch names it: it cannot be changed in /nonexistent/ws)", shown
    assert shown[1:] == [str(os.path.realpath("/nonexistent/ws/notes.txt"))], shown
    assert mpp._patch_targets_display({"patch": ""}, {"workspace": "/nonexistent/ws"}) == []


def test_the_display_is_the_same_off_windows(machine, monkeypatch) -> None:
    """The card's resource list is computed the same on every host (an all-OS fix)."""
    context = {"workspace": str(machine["proj"])}
    on = mpp._affected_resources_display({"patch": DIFF}, context, intent="workspace.apply_unified_diff")
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    off = mpp._affected_resources_display({"patch": DIFF}, context, intent="workspace.apply_unified_diff")

    assert on == off == [str((machine["proj"] / "calc.py").resolve())]
