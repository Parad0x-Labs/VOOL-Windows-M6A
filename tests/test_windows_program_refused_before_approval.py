"""On Windows, a program VOOL never runs is refused plainly, before any approval card or model call.

Clean-VM capability tests C12 and C7 (0.7.1-beta, qwen3:8b, CPU only): "run calc.exe" thought for
about three minutes, showed an approval card for `sandbox.run_command`, and only after "Allow once"
said it could not; "run cmd /c dir", "run powershell Get-Process" and "run node -e ..." showed the
same card and then stopped. The 0.7.1 limits say the sandbox runs only Python and pytest on Windows:
the job runner's phase-1 gate (`JobRunner._windows_phase1_argv`) refuses every other program, but it
was asked only when the job started, after the model round and the approval.

These tests pin that the same gate is asked earlier, at two places:
- the pre-decision check every tool call passes (`windows_write_refusal_before_decision`), so a
  `sandbox.run_command`, validation or step call is refused before `decide_tool_call` and no approval
  request is created;
- the turn's front door, so a "run <program>" message is answered before any model lane runs.
Python and pytest still reach the normal path (the approval card in Manual mode).

The Windows decision is switched on through the job runner's own seam (`_is_windows_platform`) and
the command-line seam (`network_guard._IS_WINDOWS`), never through `os.name`.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from core import mode_permission_policy
from core import runtime_execution_tools as rte
from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from sandbox import job_runner, network_guard
from tests import _windows_refusals as windows_refusals

#: Each program form the capability test sent (and the 0.7.1 limits name), with the program the
#: refusal names.
REFUSED_COMMANDS = {
    "calc.exe": "calc.exe",
    "calc": "calc",
    "C:\\Windows\\System32\\calc.exe": "C:\\Windows\\System32\\calc.exe",
    "cmd /c dir": "cmd",
    "cmd.exe /c dir": "cmd.exe",
    "powershell Get-Process": "powershell",
    "pwsh -NoProfile -Command Get-Process": "pwsh",
    'node -e "console.log(1)"': "node",
    "npm install": "npm",
    "npx create-react-app x": "npx",
    "ruff check .": "ruff",
    "pip install requests": "pip",
    "git status": "git",
    "cargo build": "cargo",
    "setup.exe /S": "setup.exe",
    "\\\\attkr9\\share\\tool.exe": "\\\\attkr9\\share\\tool.exe",
}
#: Python and pytest commands phase 1 runs: they reach the normal path.
ALLOWED_COMMANDS = ["python -c pass", "python hello.py", "py hello.py", "pytest -q", "python3 -m pytest -q"]


def _plain(program: str) -> str:
    return (
        f"On Windows, VOOL 0.7.1 runs only Python and pytest in its sandbox; it doesn't run {program}. "
        "Use WSL2/Linux for that. Nothing was run."
    )


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Every permission decision and approval request taken, and every job the runner starts."""
    calls: dict[str, list] = {"decisions": [], "approvals": [], "jobs": []}
    real_decide = executor.decide_tool_call
    real_new = mode_permission_policy._new_approval_request

    def decide(**kwargs):
        calls["decisions"].append(kwargs.get("intent"))
        return real_decide(**kwargs)

    def new_request(**kwargs):
        calls["approvals"].append(kwargs.get("intent"))
        return real_new(**kwargs)

    def run(self, argv, **_kwargs):
        calls["jobs"].append(list(argv))
        raise AssertionError("no job may start in these tests")

    monkeypatch.setattr(executor, "decide_tool_call", decide)
    monkeypatch.setattr(mode_permission_policy, "_new_approval_request", new_request)
    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return calls


def _execute(intent: str, arguments: dict, workspace: Path):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"program-{uuid.uuid4().hex}",
        session_id=f"program-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context={"workspace": str(workspace), "operating_mode": "manual"},
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


# --- the gate's own verdict, asked early ---------------------------------------------------------


def test_the_phase1_gate_refuses_every_program_but_python_and_pytest_in_plain_words(windows) -> None:
    refused = {command: job_runner.windows_program_refusal(network_guard.parse_command(command) if not command.startswith("\\\\") else [command]) for command in REFUSED_COMMANDS}

    assert refused == {command: _plain(program) for command, program in REFUSED_COMMANDS.items()}
    for command in ALLOWED_COMMANDS:
        assert job_runner.windows_program_refusal(network_guard.parse_command(command)) is None, command
    # The interpreter running VOOL, named by its full path, is this interpreter.
    assert job_runner.windows_program_refusal([sys.executable, "-c", "pass"]) is None


def test_off_windows_nothing_is_refused_early(recorded) -> None:
    if job_runner._is_windows_platform():
        # A real Windows host is not off Windows: the 0.7.1 limit holds there, and the same calls
        # refuse calc.exe in plain words before anything runs.
        assert job_runner.windows_program_refusal(["calc.exe"]) == _plain("calc.exe")
        early = rte.windows_program_refusal("sandbox.run_command", {"command": "calc.exe"})
        assert early is not None and early.status == rte.WINDOWS_PROGRAM_STATUS, early
        assert early.response_text == _plain("calc.exe")
        assert rte.windows_program_check_applies("sandbox.run_command")
        assert recorded == {"decisions": [], "approvals": [], "jobs": []}
        return
    assert job_runner.windows_program_refusal(["calc.exe"]) is None
    assert rte.windows_program_refusal("sandbox.run_command", {"command": "calc.exe"}) is None
    assert not rte.windows_program_check_applies("sandbox.run_command")


def test_the_early_verdict_is_the_job_runners_gate_not_a_second_list(windows, monkeypatch) -> None:
    """When the gate refuses a Python command line (as `windows/port-captest-python-argv-allowlist`
    makes it refuse `-m pip` and `-m ruff`), the early check gives the gate's own sentence: the
    program runs there, only that command line does not."""
    real = job_runner.JobRunner._windows_phase1_argv

    def gate(argv):
        if list(argv[1:3]) in (["-m", "pip"], ["-m", "ruff"]):
            raise job_runner.KernelIsolationUnavailableError(f"gate refuses {argv[1]} {argv[2]}")
        return real(argv)

    monkeypatch.setattr(job_runner.JobRunner, "_windows_phase1_argv", staticmethod(gate))

    assert job_runner.windows_program_refusal(["python", "-m", "pip", "install", "requests"]) == "gate refuses -m pip"
    assert job_runner.windows_program_refusal(["python", "-m", "pytest"]) is None
    # The validation intents are judged by the command `_run_validation` runs (`<python> -m ruff`).
    lint = rte.windows_program_refusal("workspace.run_lint", {})
    assert lint is not None and lint.response_text == "gate refuses -m ruff"
    pip = rte.windows_program_refusal("sandbox.run_command", {"command": "python -m pip install requests"})
    assert pip is not None and pip.response_text == "gate refuses -m pip"


# --- the tool call: refused before the permission decision ---------------------------------------


@pytest.mark.parametrize("command", list(REFUSED_COMMANDS))
def test_a_run_command_for_a_program_windows_never_runs_is_refused_before_any_approval(
    windows, workspace: Path, recorded, command: str
) -> None:
    with windows_refusals.watch_git(workspace) as seen:
        execution = _execute("sandbox.run_command", {"command": command}, workspace)

    if REFUSED_COMMANDS[command] == "git" and seen.refused(execution.response_text):
        # Windows 0.7.1 runs no git: the git refusal comes first, before the program check.
        assert execution.ok is False and execution.status == gate.STATUS, (execution.status, execution.response_text)
        assert recorded == {"decisions": [], "approvals": [], "jobs": []}
        return
    assert execution.status == rte.WINDOWS_PROGRAM_STATUS, (execution.status, execution.response_text)
    assert execution.ok is False
    assert execution.response_text == _plain(REFUSED_COMMANDS[command])
    assert recorded == {"decisions": [], "approvals": [], "jobs": []}


@pytest.mark.parametrize(
    ("intent", "arguments", "program"),
    [
        ("workspace.run_tests", {"command": "node test.js"}, "node"),
        ("workspace.run_lint", {"command": "npx eslint ."}, "npx"),
        ("workspace.run_formatter", {"command": "npm run format"}, "npm"),
        ("code.task.step", {"task_id": "ct-x", "intent": "sandbox.run_command", "arguments": {"command": "calc.exe"}}, "calc.exe"),
    ],
)
def test_a_validation_or_step_call_for_such_a_program_is_refused_before_any_approval(
    windows, workspace: Path, recorded, intent: str, arguments: dict, program: str
) -> None:
    execution = _execute(intent, arguments, workspace)

    assert execution.status == rte.WINDOWS_PROGRAM_STATUS, (execution.status, execution.response_text)
    assert execution.response_text == _plain(program)
    assert recorded == {"decisions": [], "approvals": [], "jobs": []}


def test_the_authorized_execution_boundary_refuses_it_before_its_decision_too(windows, workspace: Path, monkeypatch) -> None:
    from core import authorized_tool_execution

    def no_decision(*_args, **_kwargs):
        raise AssertionError("no permission decision may be taken")

    monkeypatch.setattr(authorized_tool_execution, "authorize_runtime_tool", no_decision)

    result = authorized_tool_execution.execute_authorized_runtime_tool(
        "sandbox.run_command", {"command": "powershell Get-Process"}, task_id="t",
        source_context={"workspace": str(workspace), "operating_mode": "manual"},
    )

    assert result.ok is False and result.status == rte.WINDOWS_PROGRAM_STATUS
    assert result.response_text == _plain("powershell")


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_python_and_pytest_still_reach_the_permission_decision(windows, workspace: Path, recorded, command: str) -> None:
    execution = _execute("sandbox.run_command", {"command": command}, workspace)

    assert execution.status == "pending_approval", (execution.status, execution.response_text)
    assert recorded["decisions"] == ["sandbox.run_command"]
    assert recorded["approvals"] == ["sandbox.run_command"]
    assert recorded["jobs"] == []


# --- the front door: answered before any model lane ----------------------------------------------

FRONT_DOOR_REQUESTS = {
    "run calc.exe": "calc.exe",
    "Run calc.exe.": "calc.exe",
    "please run cmd /c dir": "cmd",
    "run powershell Get-Process": "powershell",
    'run node -e "console.log(1)"': "node",
    "run npm install": "npm",
    "execute C:\\Windows\\System32\\calc.exe": "C:\\Windows\\System32\\calc.exe",
    "launch notepad.exe": "notepad.exe",
    "start calc": "calc",
    "run `git status`": "git",
    "lint this with ruff": "ruff",
    "can you run pip install requests": "pip",
}
#: Messages the front door leaves to the lanes that own them: Python/pytest, and no program named.
FRONT_DOOR_PASSES = [
    "run python hello.py",
    "run pytest -q",
    "run the tests",
    "start a new project",
    "launch the builder",
    "run this",
    "run calc.exe\nand then tell me what you see",
    "what does calc.exe do",
    "how do I run node on Windows",
]


def test_the_front_door_reads_a_run_request_and_asks_the_tool_calls_own_check(windows) -> None:
    from core.agent_runtime.windows_program_request import windows_program_request_refusal

    with windows_refusals.watch_git() as seen:
        answers = {text: windows_program_request_refusal(text) for text in FRONT_DOOR_REQUESTS}

    expected = {text: _plain(program) for text, program in FRONT_DOOR_REQUESTS.items()}
    if seen.refused(answers["run `git status`"]):
        # Windows 0.7.1 runs no git: a git request is answered with the git sentence, not the program one.
        expected["run `git status`"] = windows_refusals.GIT_REFUSAL
    assert answers == expected
    assert {text: windows_program_request_refusal(text) for text in FRONT_DOOR_PASSES} == dict.fromkeys(FRONT_DOOR_PASSES, "")


def test_off_windows_the_front_door_answers_no_run_request() -> None:
    from core.agent_runtime.windows_program_request import windows_program_request_refusal

    if job_runner._is_windows_platform():
        # A real Windows host is not off Windows: the front door answers the request with the limit.
        assert windows_program_request_refusal("run calc.exe") == _plain("calc.exe")
        return
    assert windows_program_request_refusal("run calc.exe") == ""


def _served_chat(monkeypatch):
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core.web.api.runtime import RuntimeServices
    from tests.asgi_harness import asgi_request

    agent = VoolAgent(backend_name="test-backend", device="program-test", persona_id="default")
    monkeypatch.setattr(vool_api_server, "_agent", agent)
    app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
    headers = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}

    def chat(text: str) -> tuple[int, str]:
        body = {"messages": [{"role": "user", "content": text}], "session_id": f"openclaw:{uuid.uuid4().hex[:20]}", "stream": False}
        status, _headers, raw = asgi_request(app, method="POST", path="/api/chat", headers=headers, body=json.dumps(body).encode("utf-8"))
        payload = json.loads(raw.decode("utf-8") or "{}")
        return status, str((payload.get("message") or {}).get("content") or "")

    return chat


@pytest.mark.parametrize("text", ["run calc.exe", "run cmd /c dir", "run powershell Get-Process", 'run node -e "console.log(1)"', "run npm install"])
def test_a_served_run_request_is_answered_before_any_model_lane_or_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows, recorded, text: str
) -> None:
    from apps.vool_agent import VoolAgent

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    model_lanes: list[str] = []
    for name in ("_maybe_answer_conductor_turn", "_prepare_turn_task_bundle", "_execute_grounded_turn"):
        real = getattr(VoolAgent, name)

        def record(self, *args, _name=name, _real=real, **kwargs):
            model_lanes.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(VoolAgent, name, record)
    chat = _served_chat(monkeypatch)

    status, reply = chat(text)

    assert status == 200
    assert reply == _plain(FRONT_DOOR_REQUESTS[text] if text in FRONT_DOOR_REQUESTS else text.split()[1])
    assert "_prepare_turn_task_bundle" not in model_lanes and "_execute_grounded_turn" not in model_lanes, model_lanes
    assert recorded == {"decisions": [], "approvals": [], "jobs": []}
