"""Windows: an approved Python or pytest job runs once, with the argv and folder the owner approved.

Captest M4 (installed build 828b8bda, fresh Windows 11 VM, Manual mode). In the workspace:
- "run pytest -q": the approval card came, the owner clicked Allow, and the reply was "wasn't able ...":
  nothing ran.
- "run python -m unittest" and "run python -m compileall -q .": one Allow, then "Command requires
  explicit user approval." The approved call was gated again as if it had never been approved.

Two things stood between the Allow and the job, both after the controller had spent the approval:
1. The sandbox command gate (`ExecutionGate.evaluate_command`) re-applies the Manual-mode matrix to
   every side-effecting command and cannot see that the controller just spent an exact approval for
   this call, so it answers "Command requires explicit user approval." on the resumed call.
2. On Windows the sandbox runner looked a bare `python`/`pytest` up on PATH before the job runner's
   phase-1 gate, which runs only the interpreter running VOOL. On a clean VM PATH holds the Store's
   `python.exe` alias or no Python at all, so the approved `python t.py` was refused as a program the
   sandbox does not run, or as "not found".

Windows is simulated the way the argv-allowlist and program-refusal tests do it: the job runner's
`_is_windows_platform` seam and the command-line seam `network_guard._IS_WINDOWS`. The job runner is
replaced by a recorder that keeps the argv and folder each job was handed, and the argv phase 1
would hand the launcher (`JobRunner._windows_phase1_argv`, which raises for a refused form the way
the real run does). The served flow is the chat page's: POST /api/chat, the card, POST /api/mode
`resolve_approval`, then the same turn resent with its approval token.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import execution_gate
from core import mode_permission_policy as mpp
from sandbox import job_runner, network_guard

HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}

#: The captest commands, each as `sandbox.run_command`, with the argv phase 1 hands the launcher after
#: the interpreter. ("pytest -q" reaches the card as `workspace.run_tests`; see its own tests below.)
COMMANDS = {
    "python t.py": ["t.py"],
    "python -m unittest": ["-m", "unittest"],
    "python -m compileall -q .": ["-m", "compileall", "-q", "."],
}


class _Result:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every job the sandbox hands the job runner: its argv, its folder and what phase 1 launches."""
    started: list[dict[str, Any]] = []

    def run(self, argv, *, cwd=None, cancel_event=None, **_kwargs):
        record: dict[str, Any] = {"argv": [str(item) for item in argv], "cwd": str(cwd or "")}
        started.append(record)
        if job_runner._is_windows_platform():
            # The real run applies the same gate in `_windows_confine_prefix`, and raises the same way.
            record["launched"] = job_runner.JobRunner._windows_phase1_argv(list(argv))
        return _Result("ran\n")

    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return started


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Windows seams on, and the clean VM's PATH: the Store's `python.exe` alias and nothing else."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    store = tmp_path / "WindowsApps"
    store.mkdir()
    for name in ("python.exe", "python3.exe"):
        (store / name).write_bytes(b"")  # an app execution alias: a file, not this interpreter
    monkeypatch.setenv("PATH", str(store))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return store


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VOOL Test" / "Allowed Ä folder"
    root.mkdir(parents=True)
    (root / "t.py").write_text("print('ran')\n", encoding="utf-8")
    (root / "test_t.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return root


def _interpreter() -> str:
    return str(Path(sys.executable).resolve())


# --- the served chat: ask, Allow, resume ---------------------------------------------------------


class _Served:
    """The chat page against the served app, with the model's tool choice scripted."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, jobs: list) -> None:
        from apps import vool_api_server
        from apps.vool_agent import VoolAgent
        from core import runtime_paths
        from core.memory_first_router import ModelExecutionDecision
        from core.web.api.runtime import RuntimeServices

        home = tmp_path / "vool-home"
        monkeypatch.setenv("VOOL_HOME", str(home))
        monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(workspace))
        runtime_paths.configure_runtime_home(home)
        mpp.reset_mode_permission_state()
        self.jobs = jobs
        self.calls: list[dict[str, Any]] = []
        self.session = f"openclaw:{uuid.uuid4().hex[:20]}"
        self.rounds = 0

        def decide(**_kwargs: Any) -> ModelExecutionDecision:
            # Like the model: it asks for the queued call until a job has run, then it answers.
            self.rounds += 1
            output = (
                self.calls[0]
                if self.calls and not self.jobs
                else {"intent": "respond.direct", "arguments": {"message": "Done."}}
            )
            return ModelExecutionDecision(
                source="provider_execution", task_hash=f"t{self.rounds}", provider_id="ollama-local:test",
                provider_name="ollama-local", model_name="test", structured_output=output,
                confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
            )

        agent = VoolAgent(backend_name="test-backend", device="approved-job", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", agent)
        monkeypatch.setattr(agent.memory_router, "resolve_tool_intent", decide)
        self.app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        from tests.asgi_harness import asgi_request

        status, _headers, raw = asgi_request(
            self.app, method="POST", path=path, headers=HEADERS, body=json.dumps(body).encode("utf-8")
        )
        payload = json.loads(raw.decode("utf-8") or "{}")
        payload["_status"] = status
        return payload

    def chat(self, text: str, *, turn_id: str, token: str = "") -> str:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}],
            "session_id": self.session,
            "turn_id": turn_id,
            "stream": False,
            "mode": "manual",
        }
        if token:
            body["approval_token"] = token
        payload = self.post("/api/chat", body)
        assert payload["_status"] == 200, payload
        return str((payload.get("message") or {}).get("content") or "")

    def pending(self, turn_id: str) -> list[dict[str, Any]]:
        return [a for a in mpp._APPROVALS.values() if a.get("status") == "pending" and a.get("task_id") == turn_id]

    def allow(self, approval_id: str) -> None:
        payload = self.post(
            "/api/mode",
            {"op": "resolve_approval", "session_id": self.session, "turn_id": "", "approval_id": approval_id,
             "decision": "allow", "scope": "once"},
        )
        assert payload["_status"] == 200 and payload["approval"]["status"] == "approved", payload

    def ask_allow_resume(
        self,
        text: str,
        call: dict[str, Any],
        *,
        resumed_text: str = "",
        resumed_call: dict[str, Any] | None = None,
        intent: str = "",
    ):
        turn_id = f"turn-{uuid.uuid4().hex[:10]}"
        self.calls[:] = [call]
        first = self.chat(text, turn_id=turn_id)
        pending = self.pending(turn_id)
        assert len(pending) == 1, first
        assert pending[0].get("intent") == (intent or call["intent"]), pending[0]
        assert self.jobs == [], "nothing may run before the owner allows it"
        token = str(pending[0]["approval_id"])
        self.allow(token)
        if resumed_call is not None:
            self.calls[:] = [resumed_call]
        resumed = self.chat(resumed_text or text, turn_id=turn_id, token=token)
        return first, resumed, token, turn_id


@pytest.fixture
def served(tmp_path, monkeypatch, workspace, jobs):
    from core import runtime_paths

    try:
        yield _Served(tmp_path, monkeypatch, workspace, jobs)
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


@pytest.mark.parametrize("command", list(COMMANDS))
def test_an_approved_python_command_runs_once_with_the_approved_argv(windows, served, workspace, command) -> None:
    call = {"intent": "sandbox.run_command", "arguments": {"command": command, "cwd": str(workspace)}}

    _first, resumed, token, _turn = served.ask_allow_resume(f"run {command}", call)

    assert "requires explicit user approval" not in resumed, resumed
    assert len(served.jobs) == 1, (served.jobs, resumed)
    job = served.jobs[0]
    program, *rest = network_guard.parse_command(command)
    # The approved argv, unchanged but for the program phase 1 pins to the interpreter running VOOL.
    assert job["argv"][1:] == rest and job["argv"][0] == program, job
    assert job["launched"] == [_interpreter(), *COMMANDS[command]], job
    assert os.path.samefile(job["cwd"], workspace), job
    assert mpp._APPROVALS[token]["status"] == "consumed"


@pytest.mark.parametrize("intent", ["workspace.run_tests", "sandbox.run_command"])
def test_an_approved_pytest_q_runs_once_in_its_folder(monkeypatch, served, workspace, intent) -> None:
    """"run pytest -q": the card asks for `workspace.run_tests` (also when the model chose
    `sandbox.run_command`), whose command names this interpreter by its full path, as on Windows
    (`C:\\...\\python.exe -m pytest -q`). A Linux interpreter path is no `X:\\...exe`, so the
    Windows command-line rules would refuse it here: only the job runner's Windows seam is on."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    call = {"intent": intent, "arguments": {"command": "pytest -q", "cwd": str(workspace)}}

    _first, resumed, token, _turn = served.ask_allow_resume("run pytest -q", call, intent="workspace.run_tests")

    assert "requires explicit user approval" not in resumed and "wasn't able" not in resumed, resumed
    assert len(served.jobs) == 1, (served.jobs, resumed)
    job = served.jobs[0]
    assert job["launched"] == [_interpreter(), "-m", "pytest", "-q"], job
    assert os.path.samefile(job["cwd"], workspace), job
    assert mpp._APPROVALS[token]["status"] == "consumed"


@pytest.mark.parametrize(
    "tampered",
    ["python evil.py", "python t.py --other", "python -m unittest", "python -m pip install requests"],
    ids=["another-script", "another-argument", "another-allowed-form", "pip"],
)
def test_a_resume_that_asks_for_another_argv_does_not_run_on_the_token(windows, served, workspace, tampered) -> None:
    """The same turn resent with the token, but asking (and the model calling) for another command."""
    (workspace / "evil.py").write_text("print('evil')\n", encoding="utf-8")
    approved = {"intent": "sandbox.run_command", "arguments": {"command": "python t.py", "cwd": str(workspace)}}
    other = {"intent": "sandbox.run_command", "arguments": {"command": tampered, "cwd": str(workspace)}}

    _first, resumed, token, turn_id = served.ask_allow_resume(
        "run python t.py", approved, resumed_text=f"run {tampered}", resumed_call=other
    )

    assert served.jobs == [], (served.jobs, resumed)
    # The grant still covers only the call the owner saw; it was not spent on the other one.
    assert mpp._APPROVALS[token]["status"] == "approved"


# --- the executor: one Allow, one run, of exactly the approved call -------------------------------


def _executor(workspace: Path, session: str, turn: str):
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
    from core.tool_intent_executor import execute_tool_intent

    context = {"runtime_session_id": session, "operating_mode": "manual", "cancel_turn_id": turn,
               "workspace": str(workspace), "workspace_root": str(workspace), "surface": "openclaw",
               "platform": "openclaw"}

    def run(arguments: dict[str, Any], token: str = ""):
        return execute_tool_intent(
            {"intent": "sandbox.run_command", "arguments": dict(arguments)},
            task_id="task-a", session_id=session,
            source_context={**context, **({"mode_approval_token": token} if token else {})},
            hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
            checkpoint_id=f"runtime-{uuid.uuid4().hex}", step_index=0,
        )

    return run


@pytest.fixture
def executor(tmp_path, monkeypatch, workspace):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    mpp.reset_mode_permission_state()
    session, turn = f"openclaw:{uuid.uuid4().hex[:12]}", "turn-a"
    mpp.set_active_mode(session, "manual", client_turn_id=turn)
    try:
        yield _executor(workspace, session, turn)
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


def _approved_token(run, arguments: dict[str, Any]) -> str:
    asked = run(arguments)
    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    token = str(asked.details["approval_request"]["approval_id"])
    assert mpp.resolve_approval(token, decision="allow") is not None
    return token


@pytest.mark.parametrize(
    "other",
    [{"command": "python t.py", "cwd": "sub"}, {"command": "python t.py -v"}, {"command": "python -c 0"}],
    ids=["another-folder", "another-argument", "another-form"],
)
def test_the_token_never_runs_another_argv_or_folder(windows, jobs, executor, workspace, other) -> None:
    (workspace / "sub").mkdir()
    approved = {"command": "python t.py", "cwd": str(workspace)}
    token = _approved_token(executor, approved)
    tampered = {**approved, **other}
    if tampered["cwd"] == "sub":
        tampered["cwd"] = str(workspace / "sub")

    refused = executor(tampered, token)

    assert refused.status == "pending_approval" and jobs == [], (refused.status, jobs)
    assert mpp._APPROVALS[token]["status"] == "approved"
    ran = executor(approved, token)
    assert ran.status == "executed", (ran.status, ran.response_text)
    assert [(job["launched"], job["cwd"]) for job in jobs] == [([_interpreter(), "t.py"], str(workspace))]


def test_one_allow_runs_the_approved_command_once(windows, jobs, executor, workspace) -> None:
    approved = {"command": "python t.py", "cwd": str(workspace)}
    token = _approved_token(executor, approved)

    first = executor(approved, token)
    replay = executor(approved, token)

    assert first.status == "executed", (first.status, first.response_text)
    assert replay.status != "executed" and len(jobs) == 1, (replay.status, jobs)


# --- the command gate honours exactly one approved command ---------------------------------------


def _gate(command: str, cwd: str) -> dict[str, str]:
    from core.effect_gateway import named_background_effect_scope

    with named_background_effect_scope(
        "approved-job-test", source_context={"operating_mode": "manual", "workspace_root": cwd}
    ):
        return execution_gate.ExecutionGate.evaluate_command(command, cwd=cwd)


def test_without_an_approval_the_gate_still_asks_in_manual_mode(tmp_path) -> None:
    assert _gate("python t.py", str(tmp_path))["decision"] == "advice_only"


def test_the_gate_lets_exactly_the_approved_command_through_once(tmp_path, monkeypatch) -> None:
    # Windows only for 0.7.1 (Security Architect, 9 Nov): the spend applies on the Windows seam.
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    cwd = str(tmp_path)
    with execution_gate.operator_approved_command("python t.py", cwd=cwd):
        assert _gate("python -m pip install x", cwd)["decision"] == "advice_only"
        assert _gate("python t.py", str(tmp_path / "elsewhere"))["decision"] == "advice_only"
        assert _gate("python t.py", cwd)["decision"] == "sandbox"
        assert _gate("python t.py", cwd)["decision"] == "advice_only", "an approval runs its command once"
    assert _gate("python t.py", cwd)["decision"] == "advice_only"


def test_the_approval_never_lifts_a_hard_block(tmp_path) -> None:
    cwd = str(tmp_path)
    with execution_gate.operator_approved_command("python t.py && rm -rf /", cwd=cwd):
        assert _gate("python t.py && rm -rf /", cwd)["decision"] == "blocked"


# --- the Windows sandbox runs the interpreter phase 1 pins, never a PATH hit ---------------------


@pytest.mark.parametrize("path_entries", ["store-alias", "nothing"])
@pytest.mark.parametrize(
    ("command", "launched"),
    [("python t.py", ["t.py"]), ("pytest -q", ["-m", "pytest", "-q"]), ("python3 -m unittest", ["-m", "unittest"])],
)
def test_a_bare_python_or_pytest_is_handed_to_phase_1_not_looked_up_on_path(
    windows, jobs, workspace, monkeypatch, path_entries, command, launched
) -> None:
    from core.effect_gateway import named_background_effect_scope
    from sandbox.sandbox_runner import SandboxRunner

    if path_entries == "nothing":
        monkeypatch.setenv("PATH", "")
    runner = SandboxRunner(execution_gate.ExecutionGate(), str(workspace))
    with named_background_effect_scope("approved-job-test", source_context={"operating_mode": "auto", "workspace_root": str(workspace)}):
        result = runner.run_command(command, cwd=str(workspace))
    assert result.get("status") == "executed", result
    assert [job["launched"] for job in jobs] == [[_interpreter(), *launched]]
    assert [job["argv"] for job in jobs] == [network_guard.parse_command(command)]


def test_a_python_planted_on_path_never_runs_and_other_programs_are_still_looked_up(
    windows, jobs, workspace, monkeypatch
) -> None:
    from core.effect_gateway import named_background_effect_scope
    from sandbox.sandbox_runner import SandboxRunner

    planted = workspace / "python.exe"
    planted.write_bytes(b"")
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{windows}")
    runner = SandboxRunner(execution_gate.ExecutionGate(), str(workspace))
    with named_background_effect_scope("approved-job-test", source_context={"operating_mode": "auto", "workspace_root": str(workspace)}):
        assert runner.run_command("python t.py", cwd=str(workspace)).get("status") == "executed"
        for command in ("node -e 1", "git status"):
            result = runner.run_command(command, cwd=str(workspace))
            assert result.get("status") == "blocked_by_policy", (command, result)
    assert [job["launched"] for job in jobs] == [[_interpreter(), "t.py"]]


# --- after the approved run, a refused follow-up still ends the turn on the command's output -----


def test_an_approved_python_run_whose_followup_is_malformed_ends_with_the_output(
    windows, served, workspace, monkeypatch
) -> None:
    """Captest (Windows 11 VM, qwen3:8b on the bundled Ollama, CPU only): "run calc.py in <marked
    folder>", Allow, the job ran, and the model's next step answered in prose where a tool call was
    required: "No usable provider reply ... malformed_tool_arguments:required native tool call is
    missing". The turn then asked the same model to write the answer and showed "Working" for 12+
    minutes. It must end on the command's output, saying the follow-up failed, without that call."""
    from apps import vool_api_server
    from core.memory_first_router import ModelExecutionDecision

    agent = vool_api_server._agent
    reason = "malformed_tool_arguments:required native tool call is missing"
    approved_call = {"intent": "sandbox.run_command", "arguments": {"command": "python t.py", "cwd": str(workspace)}}
    synthesis: list[dict[str, Any]] = []

    def decide(**kwargs: Any) -> ModelExecutionDecision:
        served.rounds += 1
        if not served.jobs:
            return ModelExecutionDecision(
                source="provider_execution", task_hash=f"t{served.rounds}", provider_id="ollama-local:test",
                provider_name="ollama-local", model_name="test", structured_output=dict(approved_call),
                confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
            )
        context = kwargs.get("source_context")
        if isinstance(context, dict):
            # What the router records on the shared context when it refuses the reply.
            context["last_tool_call_rejection"] = {
                "error_kind": "malformed_tool_arguments", "provider_id": "ollama-local:test", "resolution": {},
            }
        return ModelExecutionDecision(
            source="selected_model_blocked", task_hash=f"t{served.rounds}", used_model=False,
            structured_output=None, validation_state="not_run",
            details={"reason": reason, "block_reason": reason, "model_was_attempted": True},
        )

    real_resolve = agent.memory_router.resolve

    def resolve(**kwargs: Any):
        if kwargs.get("force_model") and served.jobs:
            synthesis.append(dict(kwargs.get("source_context") or {}))
        return real_resolve(**kwargs)

    monkeypatch.setattr(agent.memory_router, "resolve_tool_intent", decide)
    monkeypatch.setattr(agent.memory_router, "resolve", resolve)

    _first, resumed, token, _turn = served.ask_allow_resume("run t.py in the folder", approved_call)

    assert len(served.jobs) == 1, (served.jobs, resumed)
    assert os.path.samefile(served.jobs[0]["cwd"], workspace), served.jobs
    assert mpp._APPROVALS[token]["status"] == "consumed"
    assert synthesis == [], "after the run, the model that just failed was asked to write the answer"
    assert "Command executed in `.`" in resumed, resumed
    assert "ran" in resumed, resumed
    assert "The model's follow-up step failed, so this is the tool output without a written answer." in resumed, resumed
    assert "malformed_tool_arguments" not in resumed, resumed
    # The approval card's preview step is not output: only what ran is shown as the result.
    assert "requires approval" not in resumed, resumed


@pytest.mark.parametrize("folder", ["", "sub"], ids=["the-workspace", "a-subfolder"])
def test_the_approved_cwd_label_is_never_blank(windows, jobs, executor, workspace, folder) -> None:
    """The captest trail read "Command executed in ``:". The label is the folder relative to the
    workspace: "." for the workspace itself, never an empty string."""
    cwd = workspace / folder if folder else workspace
    cwd.mkdir(exist_ok=True)
    approved = {"command": "python t.py", "cwd": str(cwd)}
    token = _approved_token(executor, approved)

    ran = executor(approved, token)

    assert ran.status == "executed", (ran.status, ran.response_text)
    first_line = ran.response_text.splitlines()[0]
    assert first_line == f"Command executed in `{folder or '.'}`:", first_line
    assert [job["cwd"] for job in jobs] == [str(cwd)]
