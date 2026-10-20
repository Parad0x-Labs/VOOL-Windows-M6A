"""An approved code-task patch applies each of its sections once, and leaves a file it does not change untouched.

A user approves a code-task patch (`workspace.apply_unified_diff`) and the task's step applies it through VOOL's
protected patch engine (`_apply_protected_unified_diff` in core/execution/workspace_tools.py), which writes only the
files the proposal recorded, each through the pinned writer. That engine used to render every section of a patch
from the file's original bytes and write each section on its own, and it rewrote a file whose rendered text was
the bytes it already held. What this file pins, on the approved code-task route:

- Two sections for one file, the second numbered on the file the first section leaves (as `git apply` reads
  them), are applied in turn: the file holds exactly the bytes of both, the result names it once in
  `changed_paths`, and the mutation record holds one change for it, from its bytes before the patch to its final
  bytes. Before, the second section was matched against the original bytes and the approved patch was refused.
- Two sections for one file whose second does not match the file the first section leaves are refused before
  any byte moves: every file keeps its bytes, its inode and its timestamp, and nothing is named as changed or
  recorded. Before, that second section matched the original bytes, so the first write landed, the second write
  found it, every file already written was written back, and the patch answered `stale_base`.
- A file whose section leaves its bytes as they are is not written: it keeps its inode and its timestamp and is
  in neither `changed_paths` nor the mutation record, while the other file of the patch changes and is recorded;
  rolling the task back puts back only that other file.

Every approval comes from the real control plane (open, read, reproduce, identify, propose, approve) through the
production door, with the rig of tests/test_approved_destination_identity_r6.py, and every write is a real write
to a disposable repository; no engine, writer or record is replaced. The rig's reproduce step runs a real,
failing check through `workspace.run_tests`, which needs the OS sandbox (bwrap on Linux, Seatbelt on macOS), so
this file runs in CI and cannot pass on a machine without one.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.execution.artifacts import latest_workspace_mutation
from tests.repoops._harness import context, door
from tests.repoops.test_forge_actions import world
from tests.test_approved_destination_identity_r6 import (
    TAX_BUGGY,
    TAX_FIXED,
    _approved_patch,
    _journal,
    _new_effects,
    _patch_step,
    _restart,
    _turn_ids,
)

RECEIPT = (
    "Receipt\n"
    "Shop: Corner Books\n"
    "Net: {net}\n"
    "VAT: {vat}\n"
    "Total: {total}\n"
    "Paid by: {method}\n"
    "Returns within 30 days\n"
    "Keep this receipt\n"
    "Questions: ask at the till\n"
    "Thank you\n"
)
RECEIPT_AFTER = (
    "Receipt\n"
    "Shop: Corner Books\n"
    "Net: {net}\n"
    "VAT rate: 20%\n"
    "VAT: {vat}\n"
    "Total: {total}\n"
    "Paid by: {method}\n"
    "Returns within 30 days\n"
    "Keep this receipt\n"
    "Questions: ask at the till or write to the shop\n"
    "Thank you\n"
)
NOTES_TEXT = "vat is charged on the net amount\n"

VAT_SECTION = (
    "diff --git a/billing/vat.py b/billing/vat.py\n"
    "--- a/billing/vat.py\n"
    "+++ b/billing/vat.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def vat(net):\n"
    "-    return net * 2\n"
    "+    return round(net * 0.2, 2)\n"
)
# The first receipt section adds a line after line 3, so every later line of the receipt moves down by one.
RATE_LINE_SECTION = (
    "diff --git a/billing/receipt.txt b/billing/receipt.txt\n"
    "--- a/billing/receipt.txt\n"
    "+++ b/billing/receipt.txt\n"
    "@@ -3,2 +3,3 @@\n"
    " Net: {net}\n"
    "+VAT rate: 20%\n"
    " VAT: {vat}\n"
)


def _questions_section(start: int) -> str:
    """The second receipt section, from line `start`: line 9 of the receipt the first section leaves, or line 8
    of the receipt before the patch."""
    return (
        "diff --git a/billing/receipt.txt b/billing/receipt.txt\n"
        "--- a/billing/receipt.txt\n"
        "+++ b/billing/receipt.txt\n"
        f"@@ -{start},3 +{start},3 @@\n"
        " Keep this receipt\n"
        "-Questions: ask at the till\n"
        "+Questions: ask at the till or write to the shop\n"
        " Thank you\n"
    )


def _receipt_world(root: Path) -> None:
    (root / "billing").mkdir()
    (root / "billing" / "vat.py").write_bytes(TAX_BUGGY.encode("utf-8"))
    (root / "billing" / "receipt.txt").write_bytes(RECEIPT.encode("utf-8"))
    (root / "billing" / "NOTES.md").write_bytes(NOTES_TEXT.encode("utf-8"))


def _identity(path: Path) -> tuple[int, int]:
    """The file's inode and timestamp: a rewrite, even with the same bytes, changes at least one of them."""
    info = os.stat(path)
    return (info.st_ino, info.st_mtime_ns)


def _billing(root: Path) -> dict[str, tuple[bytes, tuple[int, int]]]:
    """Every entry of the billing folder, with its bytes and its identity."""
    folder = root / "billing"
    return {name: ((folder / name).read_bytes(), _identity(folder / name)) for name in sorted(os.listdir(folder))}


def _writer(result) -> dict:
    return dict(result.details.get("tool_result") or {})


def _recorded_changes(ctx: dict, writer: dict) -> list[tuple[str, str, str, str]]:
    """The changes of the mutation record the writer's result names: (path, action, text before, text after)."""
    record = latest_workspace_mutation(session_id=ctx["session_id"], workspace_root=Path(ctx["workspace_root"]))
    assert record is not None, writer
    assert record["mutation_id"] == writer["mutation_record"]["mutation_id"], (record, writer)
    return [(change["path"], change["action"], change["before_text"], change["after_text"]) for change in record["changes"]]


def test_an_approved_patch_with_two_sections_for_one_file_applies_both_and_records_the_file_once(world) -> None:
    root, _bare, _forge = world
    _receipt_world(root)
    ctx = context(root, session="approved-patch-two-sections")
    task_id, args = _approved_patch(ctx, VAT_SECTION + RATE_LINE_SECTION + _questions_section(9))
    recorded = _journal(task_id)["proposals"]["patch"]["preview"]["destinations"]
    assert sorted(row["path"] for row in recorded) == ["billing/receipt.txt", "billing/vat.py"], recorded
    _restart()
    turns_before = _turn_ids()
    applied = door("code.task.step", _patch_step(task_id, args), ctx)
    writer = _writer(applied)
    assert (applied.ok, applied.status) == (True, "executed"), (applied.status, applied.response_text)
    assert (root / "billing" / "receipt.txt").read_bytes() == RECEIPT_AFTER.encode("utf-8")
    assert (root / "billing" / "vat.py").read_bytes() == TAX_FIXED.encode("utf-8")
    assert (root / "billing" / "NOTES.md").read_bytes() == NOTES_TEXT.encode("utf-8")
    assert writer["engine"] == "protected_python", writer
    assert writer["changed_paths"] == ["billing/vat.py", "billing/receipt.txt"], writer
    # One change for the receipt, from its bytes before the patch to its final bytes: never two writes, each
    # rendered from the original.
    assert _recorded_changes(ctx, writer) == [
        ("billing/vat.py", "updated", TAX_BUGGY, TAX_FIXED),
        ("billing/receipt.txt", "updated", RECEIPT, RECEIPT_AFTER),
    ]
    assert _new_effects(turns_before) == [
        ("billing/receipt.txt", "succeeded", "executed"),
        ("billing/vat.py", "succeeded", "executed"),
    ]


def test_an_approved_patch_whose_second_section_does_not_match_what_the_first_left_changes_no_file(world) -> None:
    root, _bare, _forge = world
    _receipt_world(root)
    ctx = context(root, session="approved-patch-second-section-mismatch")
    # Numbered on the receipt before the patch: on the receipt the first section leaves, line 8 says
    # "Returns within 30 days", not "Keep this receipt".
    task_id, args = _approved_patch(ctx, VAT_SECTION + RATE_LINE_SECTION + _questions_section(8))
    before = _billing(root)
    turns_before = _turn_ids()
    refused = door("code.task.step", _patch_step(task_id, args), ctx)
    writer = _writer(refused)
    # Never written, not written and then written back: every file keeps its bytes, its inode and its timestamp.
    assert _billing(root) == before, (refused.status, refused.response_text)
    assert (refused.ok, refused.status) == (False, "protected_patch_unsupported"), (refused.status, refused.response_text)
    assert "context mismatch for `receipt.txt`" in refused.response_text, refused.response_text
    assert writer["changed_paths"] == [] and "mutation_record" not in writer, writer
    assert latest_workspace_mutation(session_id=ctx["session_id"], workspace_root=root) is None
    assert _journal(task_id)["steps"]["apply-patch"]["status"] == "protected_patch_unsupported"
    assert _new_effects(turns_before) == [
        ("billing/receipt.txt", "refused", "protected_patch_unsupported"),
        ("billing/vat.py", "refused", "protected_patch_unsupported"),
    ]


def test_an_approved_patch_leaves_a_file_its_section_does_not_change_unwritten_and_unrecorded(world) -> None:
    root, _bare, _forge = world
    _receipt_world(root)
    ctx = context(root, session="approved-patch-unchanged-file")
    same_line = (
        "diff --git a/billing/receipt.txt b/billing/receipt.txt\n"
        "--- a/billing/receipt.txt\n"
        "+++ b/billing/receipt.txt\n"
        "@@ -9,2 +9,2 @@\n"
        " Questions: ask at the till\n"
        "-Thank you\n"
        "+Thank you\n"
    )
    task_id, args = _approved_patch(ctx, VAT_SECTION + same_line)
    receipt = root / "billing" / "receipt.txt"
    identity = _identity(receipt)
    turns_before = _turn_ids()
    applied = door("code.task.step", _patch_step(task_id, args), ctx)
    writer = _writer(applied)
    assert (applied.ok, applied.status) == (True, "executed"), (applied.status, applied.response_text)
    assert (root / "billing" / "vat.py").read_bytes() == TAX_FIXED.encode("utf-8")
    # Not rewritten: the same file with its timestamp, so an editor or a build sees no change either.
    assert receipt.read_bytes() == RECEIPT.encode("utf-8") and _identity(receipt) == identity
    assert writer["changed_paths"] == ["billing/vat.py"], writer
    assert _recorded_changes(ctx, writer) == [("billing/vat.py", "updated", TAX_BUGGY, TAX_FIXED)]
    assert _new_effects(turns_before) == [
        ("billing/receipt.txt", "no_change", "executed"),
        ("billing/vat.py", "succeeded", "executed"),
    ]

    # Undoing the repair puts back the file the patch changed and still leaves the other one as it was.
    rolled = door("code.task.rollback", {"task_id": task_id}, ctx)
    assert rolled.ok, (rolled.status, rolled.response_text)
    assert rolled.details["restored_paths"] == ["billing/vat.py"], rolled.details
    assert (root / "billing" / "vat.py").read_bytes() == TAX_BUGGY.encode("utf-8")
    assert receipt.read_bytes() == RECEIPT.encode("utf-8") and _identity(receipt) == identity
