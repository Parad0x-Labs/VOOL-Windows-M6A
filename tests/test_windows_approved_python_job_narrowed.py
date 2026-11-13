"""The approved-job spend path, narrowed (Security Architect, 9 Nov).

On top of tests/test_windows_approved_python_job_runs_once.py:
- A mode that DENIES a command's action refuses it even under a live operator-approved scope: an
  approval lifts only REQUIRE_APPROVAL.
- A task grant lets the gate through only for the call whose fingerprint it was granted for: a grant
  from `python a.py` lets neither `python -c ...` nor `rm x` skip the gate.
- Windows only for 0.7.1: on a host that is not Windows the gate is exactly as on base, approved
  scope or not.
- Auto mode is recorded as it is today (not changed by this car).
- A `code.task.step` whose inner `workspace.run_tests` reaches the handler without crossing the
  authorized boundary with its own exact approval fails closed: no job starts.

Windows is simulated through the job runner's `_is_windows_platform` seam (and, for bare program
names, `network_guard._IS_WINDOWS`), as the argv-allowlist and program-refusal tests do.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import execution_gate
from core import mode_permission_policy as mpp
from sandbox import job_runner, network_guard


class _Result:
    returncode = 0
    stdout = "ran\n"
    stderr = ""


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    started: list[dict[str, Any]] = []

    def run(self, argv, *, cwd=None, cancel_event=None, **_kwargs):
        record: dict[str, Any] = {"argv": [str(item) for item in argv], "cwd": str(cwd or "")}
        started.append(record)
        if job_runner._is_windows_platform():
            record["launched"] = job_runner.JobRunner._windows_phase1_argv(list(argv))
        return _Result()

    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return started


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("t.py", "a.py"):
        (root / name).write_text("print('ran')\n", encoding="utf-8")
    (root / "x").write_text("x\n", encoding="utf-8")
    return root


def _gate(command: str, cwd: str, *, mode: str = "manual") -> dict[str, str]:
    from core.effect_gateway import named_background_effect_scope

    with named_background_effect_scope("narrowed-test", source_context={"operating_mode": mode, "workspace_root": cwd}):
        return execution_gate.ExecutionGate.evaluate_command(command, cwd=cwd)


# --- 1. DENY stays DENY ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["python t.py", "rm x"])
def test_a_mode_that_denies_the_command_refuses_it_under_a_live_approved_scope(windows, workspace, command) -> None:
    cwd = str(workspace)
    with execution_gate.operator_approved_command(command, cwd=cwd):
        verdict = _gate(command, cwd, mode="plan")
    assert verdict["decision"] != "sandbox", verdict
    # The scope was not spent on the denied command: it still lets the same command through once in a
    # mode that only asks, so the refusal above is the DENY, not a used-up scope.
    with execution_gate.operator_approved_command("python t.py", cwd=cwd):
        assert _gate("python t.py", cwd, mode="plan")["decision"] != "sandbox"
        assert _gate("python t.py", cwd, mode="manual")["decision"] == "sandbox"


def test_without_approval_the_denying_mode_answers_as_before(windows, workspace) -> None:
    verdict = _gate("python t.py", str(workspace), mode="plan")
    assert verdict["decision"] == "advice_only" and verdict["reason"] == "Command requires explicit user approval."


# --- 2. a task grant covers only the call it was granted for ---------------------------------------


def _context(session: str, turn: str, workspace: Path, token: str = "") -> dict[str, Any]:
    context = {"runtime_session_id": session, "operating_mode": "manual", "cancel_turn_id": turn,
               "workspace": str(workspace), "workspace_root": str(workspace), "surface": "openclaw",
               "platform": "openclaw"}
    if token:
        context["mode_approval_token"] = token
    return context


def _run(arguments: dict[str, Any], context: dict[str, Any], intent: str = "sandbox.run_command"):
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
    from core.tool_intent_executor import execute_tool_intent

    return execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id="task-a", session_id=str(context["runtime_session_id"]), source_context=context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
        checkpoint_id=f"runtime-{uuid.uuid4().hex}", step_index=0,
    )


@pytest.fixture
def manual(tmp_path, monkeypatch):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    mpp.reset_mode_permission_state()
    session, turn = f"openclaw:{uuid.uuid4().hex[:12]}", "turn-a"
    mpp.set_active_mode(session, "manual", client_turn_id=turn)
    try:
        yield session, turn
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


@pytest.mark.parametrize("other", ["python -c pass", "rm x"])
def test_a_task_grant_for_one_command_lets_no_other_command_skip_the_gate(windows, jobs, workspace, manual, other) -> None:
    session, turn = manual
    context = _context(session, turn, workspace)
    granted = {"command": "python a.py", "cwd": str(workspace)}
    asked = _run(granted, context)
    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    token = str(asked.details["approval_request"]["approval_id"])
    assert mpp.resolve_approval(token, decision="allow", scope="task") is not None

    ran = _run(granted, _context(session, turn, workspace, token))
    assert ran.status == "executed", (ran.status, ran.response_text)
    for context_for_other in (_context(session, turn, workspace), _context(session, turn, workspace, token)):
        result = _run({"command": other, "cwd": str(workspace)}, context_for_other)
        assert result.status != "executed", (other, result.status, result.response_text)
    assert [job["argv"][1:] for job in jobs] == [["a.py"]], jobs
    assert (workspace / "x").exists()


def test_the_task_grant_decision_is_exact_only_for_its_own_fingerprint(workspace, manual) -> None:
    session, turn = manual
    context = _context(session, turn, workspace)
    granted = {"command": "python a.py", "cwd": str(workspace)}
    request = mpp.decide_tool_call(intent="sandbox.run_command", arguments=granted, task_id="task-a",
                                   source_context=context).approval_request
    assert mpp.resolve_approval(str(request["approval_id"]), decision="allow", scope="task") is not None

    token = str(request["approval_id"])
    for other in ("python -c pass", "rm x"):
        for ctx in (context, {**context, "mode_approval_token": token}):
            decision = mpp.decide_tool_call(intent="sandbox.run_command", arguments={"command": other, "cwd": str(workspace)},
                                            task_id="task-a", source_context=ctx)
            assert not (decision.effect is mpp.PermissionEffect.ALLOW and decision.exact_approval), (other, decision)
    # The granted call itself: exact when it spends the live token, and not when it rides the grant
    # again afterwards (that re-emission is allowed by the controller but the gate asks again).
    spent = mpp.decide_tool_call(intent="sandbox.run_command", arguments=granted, task_id="task-a",
                                 source_context={**context, "mode_approval_token": token})
    assert spent.effect is mpp.PermissionEffect.ALLOW and spent.exact_approval is True
    again = mpp.decide_tool_call(intent="sandbox.run_command", arguments=granted, task_id="task-a", source_context=context)
    assert again.effect is mpp.PermissionEffect.ALLOW and again.exact_approval is False


# --- (a) Windows only ------------------------------------------------------------------------------


@pytest.fixture
def off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that is not Windows, on any host. The spend path reads one seam, the job runner's own
    platform check (`execution_gate.operator_approval_spend_applies`): on a real Windows runner it is
    True unless it is turned off here."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)


def test_off_windows_a_live_approved_scope_changes_nothing(off_windows, workspace) -> None:
    assert not job_runner._is_windows_platform()
    assert execution_gate.operator_approval_spend_applies() is False
    cwd = str(workspace)
    before = _gate("python t.py", cwd)
    with execution_gate.operator_approved_call("sandbox.run_command"):
        assert execution_gate.claim_operator_approved_call({"sandbox.run_command"}) is False
    with execution_gate.operator_approved_command("python t.py", cwd=cwd):
        assert _gate("python t.py", cwd) == before
    assert before["decision"] == "advice_only" and before["reason"] == "Command requires explicit user approval."


def test_off_windows_an_approved_command_is_asked_about_again_as_on_base(off_windows, jobs, workspace, manual) -> None:
    session, turn = manual
    approved = {"command": "python t.py", "cwd": str(workspace)}
    asked = _run(approved, _context(session, turn, workspace))
    token = str(asked.details["approval_request"]["approval_id"])
    assert mpp.resolve_approval(token, decision="allow") is not None

    resumed = _run(approved, _context(session, turn, workspace, token))

    assert resumed.status == "user_action_required", (resumed.status, resumed.response_text)
    assert "Command requires explicit user approval." in resumed.response_text
    assert jobs == []


# --- (c) Auto mode, recorded as it is today --------------------------------------------------------


def test_auto_mode_today_a_python_command_is_refused_by_the_command_gate(windows, jobs, workspace, tmp_path, monkeypatch) -> None:
    """Recorded, not changed by this car: in Auto mode the controller allows `python t.py`, and the
    served turn's autonomy is "auto", which `_command_requires_approval` reads as `not read_only`, so
    the gate answers "Command requires explicit user approval." and no job starts."""
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    mpp.reset_mode_permission_state()
    token = execution_gate.set_request_autonomy_override("auto")
    try:
        session = f"openclaw:{uuid.uuid4().hex[:12]}"
        mpp.set_active_mode(session, "auto", client_turn_id="turn-a")
        context = {**_context(session, "turn-a", workspace), "operating_mode": "auto", "autonomy_override": "auto"}
        result = _run({"command": "python t.py", "cwd": str(workspace)}, context)
    finally:
        execution_gate.reset_request_autonomy_override(token)
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)
    assert not (result.details or {}).get("approval_request"), "Auto mode asks no card"
    assert result.status == "user_action_required", (result.status, result.response_text)
    assert "Command requires explicit user approval." in result.response_text
    assert jobs == []


# --- (d) a code task step's inner run_tests ------------------------------------------------------


def test_a_code_task_steps_inner_run_tests_fails_closed_after_the_steps_approval(jobs, workspace, manual, monkeypatch) -> None:
    """The step is approved and dispatched through the authorized boundary as `code.task.step`; the
    code task runs its inner `workspace.run_tests` through `execute_runtime_tool` directly, so that
    command carries no exact approval of its own and the gate still asks. Nothing runs."""
    from core.runtime_execution_tools import execute_runtime_tool

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setenv("VOOL_CODE_TASK_DIR", str(workspace.parent / "code_tasks"))
    (workspace / "test_t.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    session, turn = manual
    context = _context(session, turn, workspace)
    opened = execute_runtime_tool("code.task.open", {"objective": "Run the tests of t.py"}, source_context=context)
    assert opened is not None and opened.details.get("task_id"), opened and opened.response_text
    step = {"task_id": opened.details["task_id"], "step_id": "s1", "intent": "workspace.run_tests",
            "arguments": {"command": "python -m pytest -q"}}

    asked = _run(step, context, intent="code.task.step")
    request = (asked.details or {}).get("approval_request") or {}
    if request.get("approval_id"):
        assert mpp.resolve_approval(str(request["approval_id"]), decision="allow") is not None
        resumed = _run(step, _context(session, turn, workspace, str(request["approval_id"])), intent="code.task.step")
    else:
        resumed = asked
    assert jobs == [], (asked.status, resumed.status, resumed.response_text, jobs)
    assert resumed.status != "executed"
    print("CODE_TASK_STEP", asked.status, "->", resumed.status, "|", (resumed.response_text or "")[:300])
