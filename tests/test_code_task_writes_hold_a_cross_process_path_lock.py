"""Two approved writes on one reviewed base never both land, across processes.

The runtime kept one writer per file only with in-process thread locks. The cross-process journal
lock is released while the writer runs, and the writer's own base check (``expected_hash``, then a
re-read just before its rename) holds no lock. A second process executing a DIFFERENT approved
proposal against the same base could pass that check between the first process's check and its
rename: CI saw the loser land and then fail its byte check (``bytes_diverged``), and with other
timing both writes reported "executed, bytes verified" while the first was silently overwritten.

These cases make that interleaving deterministic at the runtime's own dispatch seam: while the
parent's approved write is about to reach the writer, a REAL second process executes another
approval on the same file -- of the same task, or of another task over the same workspace. The
second process must be refused unexecuted, the parent's reviewed write must land, and the journal
must show exactly one consumed approval.

The task's reproduce stage needs a sandboxed command run, which the Windows runner cannot make, and
the lock under test is the one that differs there (``msvcrt.locking``, not ``flock``). So in the
parent only, ``workspace.run_tests`` runs the same check as a plain subprocess with this Python; the
racing writes in both processes go through the production door unchanged.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from tests.test_code_task_reviewed_base import (
    BUGGY,
    FIXED,
    _approved,
    _door,
    _journal,
    _race_processes,
    _sha,
    stock_repo,
)

assert stock_repo  # the fixture, imported for these cases

SECOND = FIXED + "# second\n"


@pytest.fixture(autouse=True)
def _reproduce_without_the_sandbox(monkeypatch):
    import core.runtime_execution_tools as tools

    door = tools.execute_runtime_tool

    def run_tests_plainly(intent, arguments, *args, **kwargs):
        if intent != "workspace.run_tests":
            return door(intent, arguments, *args, **kwargs)
        context = kwargs.get("source_context") or {}
        command = arguments["command"]
        script = command.split()[-1]
        ran = subprocess.run([sys.executable, script], cwd=context.get("workspace_root"),
                             capture_output=True, text=True, timeout=60)
        ok = ran.returncode == 0
        return tools.RuntimeExecutionResult(
            handled=True, ok=ok, status="executed" if ok else "command_failed",
            response_text=ran.stdout + ran.stderr,
            details={"command": command, "returncode": ran.returncode, "stdout": ran.stdout,
                     "stderr": ran.stderr, "success": ok, "executed": True},
        )

    monkeypatch.setattr(tools, "execute_runtime_tool", run_tests_plainly)


def _parent_writes_while_another_process_writes(monkeypatch, tmp_path, ctx, competing: dict):
    from core.code_assistant.task_runtime import code_task_runtime

    runtime = code_task_runtime()
    original = runtime._dispatch
    seen: dict[str, dict] = {}

    def dispatch_after_a_competing_process(intent, dispatched, context):
        if intent == "workspace.write_file" and "child" not in seen:
            seen["child"] = _race_processes(tmp_path, competing["ctx"], [competing["step"]])[0]
        return original(intent, dispatched, context)

    monkeypatch.setattr(runtime, "_dispatch", dispatch_after_a_competing_process)
    return seen


def test_another_approval_of_the_same_task_cannot_write_the_file_mid_flight(stock_repo, tmp_path, monkeypatch):
    ctx, task_id, arguments, step = _approved(stock_repo, intent="workspace.write_file", explicit_hash=True,
                                              session="path-lock-same-task")
    other = {"path": "inventory.py", "content": SECOND, "expected_hash": _sha(BUGGY)}
    assert _door("code.task.propose", {"task_id": task_id, "proposal_id": "second", "intent": "workspace.write_file",
                                       "arguments": other, "rationale": "Owner inventory.py: add deliveries."}, ctx).ok
    assert _door("code.task.approve", {"task_id": task_id, "proposal_id": "second"}, ctx).ok
    seen = _parent_writes_while_another_process_writes(monkeypatch, tmp_path, ctx, {
        "ctx": ctx,
        "step": {"task_id": task_id, "step_id": "from-another-process", "intent": "workspace.write_file",
                 "arguments": other},
    })

    landed = step("parent", "workspace.write_file", arguments)

    assert seen["child"] == {"ok": False, "status": "approval_in_flight", "executed": False}, seen
    assert landed.ok, (landed.status, landed.response_text)
    assert (stock_repo / "inventory.py").read_text(encoding="utf-8") == FIXED
    journal = _journal(task_id)
    assert journal["steps"]["from-another-process"]["status"] == "approval_in_flight"
    assert not journal["steps"]["from-another-process"]["executed"]
    assert journal["proposals"]["restock-fix"]["consumed_by"] == "parent"
    second = journal["proposals"]["second"]
    assert not second["consumed_by"] and not second["reserved_by"], second
    executed = [s["step_id"] for s in journal["steps"].values()
                if s["executed"] and s["intent"] == "workspace.write_file"]
    assert executed == ["parent"], executed


def test_an_approval_of_another_task_cannot_write_the_file_mid_flight(stock_repo, tmp_path, monkeypatch):
    ctx, task_id, arguments, step = _approved(stock_repo, intent="workspace.write_file", explicit_hash=True,
                                              session="path-lock-task-one")
    other_ctx, other_task, other_arguments, _other_step = _approved(
        stock_repo, intent="workspace.write_file", explicit_hash=True, session="path-lock-task-two",
        content=SECOND, proposal_id="other-fix",
    )
    seen = _parent_writes_while_another_process_writes(monkeypatch, tmp_path, ctx, {
        "ctx": other_ctx,
        "step": {"task_id": other_task, "step_id": "from-another-task", "intent": "workspace.write_file",
                 "arguments": other_arguments},
    })

    landed = step("parent", "workspace.write_file", arguments)

    assert seen["child"] == {"ok": False, "status": "approval_in_flight", "executed": False}, seen
    assert landed.ok, (landed.status, landed.response_text)
    assert (stock_repo / "inventory.py").read_text(encoding="utf-8") == FIXED
    assert _journal(task_id)["proposals"]["restock-fix"]["consumed_by"] == "parent"
    other_journal = _journal(other_task)
    assert other_journal["steps"]["from-another-task"]["status"] == "approval_in_flight"
    assert not other_journal["steps"]["from-another-task"]["executed"]
    assert not other_journal["proposals"]["other-fix"]["consumed_by"]


def test_an_approved_write_with_no_competitor_still_lands(stock_repo):
    """The lock only orders writers: it never refuses a write nobody else is making."""
    ctx, task_id, arguments, step = _approved(stock_repo, intent="workspace.write_file", explicit_hash=True,
                                              session="path-lock-alone")
    landed = step("alone", "workspace.write_file", arguments)
    assert landed.ok, (landed.status, landed.response_text)
    assert (stock_repo / "inventory.py").read_text(encoding="utf-8") == FIXED
    assert _journal(task_id)["proposals"]["restock-fix"]["consumed_by"] == "alone"
