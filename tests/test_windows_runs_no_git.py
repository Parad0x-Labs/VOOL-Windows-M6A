"""Windows 0.7.1: VOOL runs no git, and says so before anyone is asked to approve git.

Stated limit of 0.7.1 for Windows: git is refused. In the installed release VM "what's the git status
of this folder" tried to start git ("workspace.git_status failed: [WinError 2]", git was not
installed), and "commit this" asked for confirmation. With git installed, an unconfined git runs a
repository's hooks and the programs its config names outside any sandbox.

Every row drives a git entry point with the Windows decision forced on (core.safe_git._REFUSE_GIT and
core.windows_write_gate._IS_WINDOWS, the modules' test seams) and proves two things: no git process
is started (subprocess.Popen is replaced by a recorder that fails the row when asked to start git,
or a fake git on PATH would leave a marker file), and the user reads the plain refusal. The last
rows guard Linux and macOS: git still runs there.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

from core import safe_git
from core import windows_write_gate as gate

REFUSAL = safe_git.GIT_REFUSED_TEXT
GIT = "git"


def _real_git(*args: str, cwd: Path) -> None:
    subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one commit, made before any Windows decision is forced."""
    root = tmp_path / "repo"
    root.mkdir()
    _real_git("init", "-q", cwd=root)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _real_git("add", "a.txt", cwd=root)
    _real_git("commit", "-q", "-m", "first", cwd=root)
    return root


@pytest.fixture
def windows(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)


@pytest.fixture
def git_starts(monkeypatch) -> list:
    """Every attempt to start git through subprocess; the attempt itself fails the row."""
    calls: list = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            if safe_git.is_git_command(args, kwargs.get("executable")):
                calls.append(args)
                raise AssertionError(f"git was started on Windows: {args!r}")
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return calls


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch) -> Path:
    """A `git` first on PATH that only leaves a marker: the marker exists iff a git process started."""
    folder = tmp_path / "fakebin"
    folder.mkdir()
    marker = tmp_path / "git-was-started"
    program = folder / GIT
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\necho fake\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    return marker


# ------------------------------------------------------------------------- the platform decision


def test_the_decision_refuses_windows_and_any_platform_it_cannot_tell_from_windows(monkeypatch) -> None:
    def decide(name: str, platform: str) -> bool:
        monkeypatch.setattr(safe_git, "os", types.SimpleNamespace(name=name))
        monkeypatch.setattr(safe_git, "sys", types.SimpleNamespace(platform=platform))
        try:
            return safe_git.git_refused_here()
        finally:
            monkeypatch.setattr(safe_git, "os", os)
            monkeypatch.setattr(safe_git, "sys", sys)

    assert decide("nt", "win32") is True
    assert decide("posix", "cygwin") is True
    assert decide("posix", "msys") is True
    assert decide("java", "java1.8") is True, "an unknown platform fails closed"
    assert decide("posix", "") is True, "an unknown platform fails closed"
    assert decide("posix", "linux") is False
    assert decide("posix", "darwin") is False

    class Unreadable:
        def __getattr__(self, _name):
            raise RuntimeError("platform state unreadable")

    monkeypatch.setattr(safe_git, "os", Unreadable())
    try:
        assert safe_git.git_refused_here() is True, "unreadable platform state fails closed"
    finally:
        monkeypatch.setattr(safe_git, "os", os)


@pytest.mark.parametrize("command", [
    ["git", "status"], ["GIT.EXE", "status"], [r"C:\Program Files\Git\cmd\git.exe", "commit"],
    ["/usr/bin/git", "log"], ["git-lfs", "pull"], [Path("/usr/bin/git"), "status"],
    '"C:\\Program Files\\Git\\cmd\\git.exe" status', "git status --short",
    r'C:\Windows\system32\cmd.exe /c "git commit -m x"', ["cmd", "/c", "git", "status"],
    ["/bin/sh", "-c", "cd x && git commit -am x"], ["powershell", "-Command", "git push"],
    ["env", "git", "status"], ["wsl", "git", "status"],
])
def test_a_git_process_is_recognised_however_it_is_spelled(command) -> None:
    assert safe_git.is_git_command(command) is True


@pytest.mark.parametrize("command", [
    [sys.executable, "-m", "pytest", "tests/test_git_tools.py"], ["python", "-c", "print('git')"],
    ["/bin/sh", "-c", "echo tests/git/x"], ["gitk-not"], ["digit"], ["cmd", "/d", "/s", "/c", r"C:\VOOL\Start_VOOL.bat"],
])
def test_other_programs_are_not_taken_for_git(command) -> None:
    assert safe_git.is_git_command(command) is False


# ------------------------------------------------------------------- front door, before approval


def _execute(intent: str, arguments: dict, source_context: dict):
    from core import tool_intent_executor as executor
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig

    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"git-{uuid.uuid4().hex}",
        session_id=f"git-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


_GIT_CALLS = {
    "workspace.git_status": {},
    "workspace.git_diff": {},
    "workspace.git_summary": {},
    "repo.session.open": {"objective": "commit this"},
    "repo.git": {"repo_session_id": "repo-1", "operation": "commit", "message": "x"},
    "repo.push": {"repo_session_id": "repo-1"},
    "repo.step": {"repo_session_id": "repo-1", "intent": "workspace.git_status", "arguments": {}},
    "code.task.step": {"task_id": "task-1", "intent": "workspace.git_diff", "arguments": {}},
    "sandbox.run_command": {"command": "git commit -am 'commit this'"},
}


@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "marked-folder"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("intent", sorted(_GIT_CALLS))
def test_a_git_call_is_refused_in_plain_words_before_any_approval(
    repo: Path, windows, git_starts, monkeypatch, intent: str, mode: str, marked: bool
) -> None:
    from core import tool_intent_executor as executor

    if marked:  # a marked disposable folder does not let git run either
        monkeypatch.setenv(gate.ENV_VAR, str(repo))
    decisions: list = []
    real = executor.decide_tool_call
    monkeypatch.setattr(executor, "decide_tool_call", lambda *a, **k: decisions.append(k) or real(*a, **k))

    execution = _execute(intent, _GIT_CALLS[intent], {"workspace": str(repo), "operating_mode": mode})

    assert execution.ok is False and execution.response_text == REFUSAL, (execution.status, execution.response_text)
    assert execution.user_safe_response_text == REFUSAL
    assert decisions == [], "no permission decision, so no approval card, for git that never runs"
    assert execution.details.get("executed") is False
    assert git_starts == []


@pytest.mark.parametrize("intent", sorted(_GIT_CALLS))
def test_the_dispatch_time_gate_refuses_the_same_calls(repo: Path, windows, git_starts, intent: str) -> None:
    from core.runtime_execution_tools import windows_write_refusal

    refusal = windows_write_refusal(intent, _GIT_CALLS[intent], source_context={"workspace": str(repo)})

    assert refusal is not None and refusal.response_text == REFUSAL
    assert git_starts == []


@pytest.mark.parametrize("command", ["cd sub && git commit -m x", "git status", "C:/Git/cmd/git.exe log"])
def test_a_sandbox_command_that_starts_git_is_refused(tmp_path: Path, windows, command: str) -> None:
    assert gate.may_refuse("sandbox.run_command") is True
    assert gate.refusal("sandbox.run_command", {"command": command}, tmp_path) == REFUSAL


def test_a_sandbox_python_command_is_left_to_the_sandbox(tmp_path: Path, windows) -> None:
    assert gate.refusal("sandbox.run_command", {"command": "python -m pytest -q"}, tmp_path) is None


# --------------------------------------------------------------------------- each git runner


def test_workspace_git_tools_refuse_without_starting_git(repo: Path, windows, git_starts) -> None:
    from core.execution.git_tools import git_diff_workspace, git_status_workspace, git_summary_workspace

    for tool in (git_status_workspace, git_diff_workspace, git_summary_workspace):
        payload = tool({}, workspace_root=repo)
        assert payload["ok"] is False and payload["status"] == safe_git.GIT_REFUSED_STATUS
        assert payload["response_text"] == REFUSAL
    assert git_starts == []


def test_the_runtime_handler_shows_the_refusal_not_a_spawn_error(repo: Path, windows, git_starts) -> None:
    from core.runtime_execution_tools import _git_status

    result = _git_status({}, workspace_root=repo)

    assert result.ok is False and result.response_text == REFUSAL
    assert "WinError" not in result.response_text and git_starts == []


def test_repoops_git_runner_refuses_every_operation(repo: Path, windows, git_starts) -> None:
    from core.repoops.gitops import GitRunner

    runner = GitRunner(root=repo)
    code, out, err = runner.run("commit", "-am", "x")
    assert (code, out) == (-1, "") and REFUSAL in err
    assert runner.is_repo() is False and runner.head() == ""
    assert runner._repository_refusal(mutating=True, argv=("commit",)) == REFUSAL
    assert runner.push_destination_refusal("origin") == REFUSAL
    assert git_starts == []


def test_repoops_local_lifecycle_refuses_without_starting_git(repo: Path, windows, git_starts) -> None:
    from core.repoops.local_lifecycle import _run

    completed = _run(str(repo), "add", "--", "a.txt", mutating=True)

    assert completed.returncode == -1 and REFUSAL in completed.stderr
    assert git_starts == []


def test_the_write_gate_does_not_ask_git_where_it_writes(repo: Path, windows, git_starts) -> None:
    assert gate._git_places(str(repo)) == REFUSAL
    assert git_starts == []


def test_safe_git_probes_refuse_before_git_starts(repo: Path, windows, git_starts) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows) as raised:
        safe_git.executable_local_config(repo)
    assert isinstance(raised.value, FileNotFoundError) and str(raised.value) == REFUSAL
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git.executable_config_for(repo, ("status",))
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git.active_hooks(repo)
    assert safe_git.lfs_endpoint_refusal(repo, ("checkout", "main")) == REFUSAL
    assert git_starts == []


def test_a_patch_is_applied_by_the_python_engine_not_by_git_apply(repo: Path, windows, git_starts) -> None:
    from core.execution.workspace_tools import apply_unified_diff_workspace

    assert safe_git.git_executable() is None
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
    payload = apply_unified_diff_workspace({"patch": patch}, workspace_root=repo, session_id=None)

    assert git_starts == []
    assert payload.get("ok") is True, payload
    assert (repo / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_project_archaeology_gives_no_git_evidence_and_starts_no_git(repo: Path, windows, git_starts) -> None:
    from core import project_archaeology

    with pytest.raises(project_archaeology.ArchaeologyInputError, match="doesn't run git on Windows"):
        project_archaeology._git_repo_for(str(repo))
    assert git_starts == []


def test_user_rule_scans_start_no_git(repo: Path, windows, git_starts) -> None:
    from core import user_rules

    assert user_rules._is_inside_repo(repo) is False
    with pytest.raises(user_rules._ScanIncomplete, match="doesn't run git on Windows"):
        user_rules._git_text(repo, "log", "-1")
    assert git_starts == []


# ------------------------------------------------- the choke point: every other spawn in-process


@pytest.fixture
def guarded(windows) -> None:
    safe_git.install_spawn_guard()  # permanent for this process, but inert once _REFUSE_GIT is reset


@pytest.mark.parametrize("start", [
    lambda: subprocess.run([GIT, "status"], capture_output=True),
    lambda: subprocess.run(f"{GIT} status", shell=True, capture_output=True),
    lambda: subprocess.run(f"cd . && {GIT} status", shell=True, capture_output=True),
    lambda: subprocess.check_output(["/bin/sh", "-c", f"{GIT} log"]),
    lambda: subprocess.Popen(["env", GIT, "status"]).wait(),
    lambda: os.system(f"{GIT} status"),
    lambda: os.posix_spawnp(GIT, [GIT, "status"], os.environ),
], ids=["run", "shell", "shell-chain", "sh-c", "env-wrapper", "os.system", "posix_spawnp"])
def test_the_spawn_guard_refuses_git_before_the_process_exists(guarded, fake_git: Path, start) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        start()
    assert not fake_git.exists(), "a git process was started"


@pytest.mark.parametrize("call", [
    "core.web.api.runtime:git_output",
    "core.build_provenance:_git",
    "core.agent_runtime.continuity_gate:git_head_sha",
])
def test_raw_git_callers_elsewhere_in_vool_start_no_git_and_degrade_as_without_git(
    repo: Path, guarded, fake_git: Path, call: str
) -> None:
    import importlib

    module_name, name = call.split(":")
    function = getattr(importlib.import_module(module_name), name)
    result = function(str(repo)) if name == "git_head_sha" else function(repo, "rev-parse", "HEAD")

    assert result == ""
    assert not fake_git.exists(), "a git process was started"


def test_the_spawn_guard_lets_every_other_program_run(guarded) -> None:
    completed = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert completed.returncode == 0 and completed.stdout.strip() == "ok"


def test_runtime_processes_arm_the_guard_and_test_runners_do_not(windows, monkeypatch) -> None:
    monkeypatch.delenv(safe_git.SPAWN_GUARD_ENV, raising=False)
    installs: list = []
    monkeypatch.setattr(safe_git, "install_spawn_guard", lambda: installs.append(True))

    assert safe_git.arm_spawn_guard_for_this_process() is False, "pytest itself is not a VOOL runtime"
    for entry in ("apps.vool_api_server", "apps.vool_cli", "installer.bundle.bundle_supervisor",
                  "core.agent_runtime.daemon"):
        monkeypatch.delenv(safe_git.SPAWN_GUARD_ENV, raising=False)
        fake_main = types.ModuleType("__main__")
        fake_main.__spec__ = types.SimpleNamespace(name=entry)  # type: ignore[assignment]
        monkeypatch.setitem(sys.modules, "__main__", fake_main)
        assert safe_git.arm_spawn_guard_for_this_process() is True, entry
        assert os.environ[safe_git.SPAWN_GUARD_ENV] == "1", "VOOL's own children arm themselves too"
    script_main = types.ModuleType("__main__")
    script_main.__file__ = str(Path(safe_git.__file__).resolve().parents[1] / "apps" / "vool_api_server.py")
    monkeypatch.setitem(sys.modules, "__main__", script_main)
    monkeypatch.delenv(safe_git.SPAWN_GUARD_ENV, raising=False)
    assert safe_git.arm_spawn_guard_for_this_process() is True, "the server run as a script"
    builder = types.ModuleType("__main__")
    builder.__file__ = str(Path(safe_git.__file__).resolve().parents[1] / "installer" / "stamp_build_source.py")
    monkeypatch.setitem(sys.modules, "__main__", builder)
    monkeypatch.delenv(safe_git.SPAWN_GUARD_ENV, raising=False)
    assert safe_git.arm_spawn_guard_for_this_process() is False, "the release build stamps its commit with git"
    monkeypatch.setenv(safe_git.SPAWN_GUARD_ENV, "1")
    assert safe_git.arm_spawn_guard_for_this_process() is True, "a child of an armed VOOL process"
    assert safe_git.arm_spawn_guard_for_this_process(runtime=True) is True
    assert len(installs) == 7


# ------------------------------------------------------------------- Linux and macOS unchanged


def test_off_windows_nothing_is_refused_or_armed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", None)
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    if os.name != "posix" or sys.platform.startswith(("win", "cygwin", "msys")):
        pytest.skip("POSIX guard")
    assert safe_git.git_refused_here() is False
    assert safe_git.arm_spawn_guard_for_this_process(runtime=True) is False
    for intent, arguments in _GIT_CALLS.items():
        assert gate.may_refuse(intent) is False
        assert gate.refusal(intent, arguments, tmp_path) is None
    assert safe_git.git_executable()


def test_off_windows_git_still_runs_through_every_runner(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    from core.execution.git_tools import git_status_workspace, git_summary_workspace
    from core.repoops.gitops import GitRunner

    status = git_status_workspace({}, workspace_root=repo)
    assert status["ok"] is True, status
    assert git_summary_workspace({}, workspace_root=repo)["ok"] is True
    runner = GitRunner(root=repo)
    assert runner.is_repo() is True and len(runner.head()) == 40
    assert safe_git.executable_local_config(repo) == []


def test_off_windows_the_installed_guard_lets_git_run(repo: Path, monkeypatch, fake_git: Path) -> None:
    safe_git.install_spawn_guard()
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)

    completed = subprocess.run([GIT, "status"], cwd=repo, capture_output=True, text=True)

    assert completed.returncode == 0 and fake_git.exists(), "git ran (the fake on PATH left its marker)"
