"""Windows: a Python or pytest ask that names a marked folder runs there, in the sandbox, through approval.

Captest C1-C3 (installed 0.7.1-beta, qwen3:8b, 2026-11-09). A new chat works in the Desktop
(`C:\\Users\\VOOLT\\OneDrive\\Desktop`). The owner marked `C:\\Users\\VOOLT\\VOOL Test\\Allowed Ä folder`,
which holds calc.py and test_calc.py. Then:

- C1/C2 "run calc.py in <folder>", "run the tests in <folder>": "outside the active workspace (Desktop)
  ... open a chat bound to the other folder".
- "run calc.py": "There is no file at `calc.py`", from a READ of calc.py in the Desktop.
- C3 "find why the test fails, fix the cause, run the tests": run_tests asked for approval, then was
  refused because its folder was outside the workspace.

The Windows decisions are driven on Linux through the gate's `_IS_WINDOWS` flag; the marked folder is
listed through VOOL_WINDOWS_DISPOSABLE_WORKSPACES, which the owner's marks feed at start. The real
VoolConfine launch cannot run here: the sandbox runner is replaced by one that records where the job
would run, which is what this change decides.
"""
from __future__ import annotations

import ntpath

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import windows_write_gate as gate

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop workspace, a marked folder with spaces and `Ä` outside it holding
    calc.py and test_calc.py, and an unmarked sibling folder holding the same files."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = tmp_path / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    marked = tmp_path / "profile" / "VOOL Test" / "Allowed Ä folder"
    unmarked = tmp_path / "profile" / "VOOL Test" / "Other folder"
    for folder in (marked, unmarked):
        folder.mkdir(parents=True)
        (folder / "calc.py").write_text(BUGGY, encoding="utf-8")
        (folder / "test_calc.py").write_text(TEST, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    return {"home": home, "workspace": workspace, "marked": marked, "unmarked": unmarked}


# --- which folder a turn works in -------------------------------------------------------------


def test_a_code_ask_naming_a_marked_folder_works_in_it(machine) -> None:
    from core import windows_marked_code_folder as placed

    marked = machine["marked"]
    for text in (f"run calc.py in {marked}", f'run the tests in "{marked}"', f"run {marked / 'calc.py'}",
                 f"find why the test fails in {marked}, fix the cause, run the tests"):
        assert placed.turn_folder(text) == str(marked), text


def test_an_unmarked_folder_or_a_plain_question_chooses_nothing(machine) -> None:
    from core import windows_marked_code_folder as placed

    assert placed.turn_folder(f"run calc.py in {machine['unmarked']}") == ""
    assert placed.turn_folder(f"run calc.py in {machine['marked'] / 'missing'}") == ""
    assert placed.turn_folder(f"run calc.py in {machine['marked']} and in {machine['unmarked']}") == ""
    assert placed.turn_folder(f"what is the weather like in {machine['marked']}") == ""


def test_a_link_inside_the_marked_folder_that_leads_out_is_not_inside_it(machine) -> None:
    from core import windows_marked_code_folder as placed

    link = machine["marked"] / "escape"
    link.symlink_to(machine["unmarked"], target_is_directory=True)
    assert placed.turn_folder(f"run calc.py in {link}") == ""


def test_a_follow_up_stays_in_the_folder_the_chat_named(machine) -> None:
    from core import windows_marked_code_folder as placed

    history = [
        {"role": "user", "content": f"run calc.py in {machine['marked']}"},
        {"role": "assistant", "content": "It printed -1."},
        {"role": "user", "content": "now run the tests"},
    ]
    assert placed.turn_folder("now run the tests", history) == str(machine["marked"])
    moved = [*history[:-1], {"role": "user", "content": f"look at {machine['unmarked']}"},
             {"role": "user", "content": "now run the tests"}]
    assert placed.turn_folder("now run the tests", moved) == ""


def test_off_windows_nothing_is_placed(machine, monkeypatch) -> None:
    from core import windows_marked_code_folder as placed

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert placed.turn_folder(f"run calc.py in {machine['marked']}") == ""


# --- "run calc.py" is not a read ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["run calc.py", "please execute calc.py", "python calc.py", "pytest -q test_calc.py"])
def test_running_a_file_is_not_served_as_a_read_of_it(text: str) -> None:
    from core.agent_runtime.fast_paths_utility import _direct_workspace_read_requests

    assert _direct_workspace_read_requests(text) == []


@pytest.mark.parametrize("text", ["read calc.py", "show run.py", "what is in run_tests.py"])
def test_reading_a_file_is_still_a_read(text: str) -> None:
    from core.agent_runtime.fast_paths_utility import _direct_workspace_read_requests

    assert _direct_workspace_read_requests(text)


def test_a_missing_file_names_the_folder_it_was_looked_for_in(machine) -> None:
    from core.runtime_execution_tools import execute_runtime_tool

    result = execute_runtime_tool(
        "workspace.read_file", {"path": "calc.py"}, source_context={"workspace": str(machine["workspace"])}
    )
    assert result is not None and result.status == "not_found"
    assert str(machine["workspace"]) in result.response_text


# --- a validation command's folder is checked before approval ------------------------------------


def test_run_tests_outside_the_workspace_is_refused_before_approval(machine, tmp_path, monkeypatch) -> None:
    """C3: run_tests asked for approval and was then refused for its folder. Like sandbox.run_command,
    a validation command whose folder is outside the workspace is now refused before any approval."""
    from core import runtime_paths
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
    from core.mode_permission_policy import reset_mode_permission_state, set_active_mode
    from core.tool_intent_executor import execute_tool_intent

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    reset_mode_permission_state()
    try:
        session = "openclaw:run-tests-cwd"
        set_active_mode(session, "manual", client_turn_id="turn-a")
        execution = execute_tool_intent(
            {"intent": "workspace.run_tests", "arguments": {"cwd": str(machine["unmarked"])}},
            task_id="task-a", session_id=session,
            source_context={"runtime_session_id": session, "operating_mode": "manual", "cancel_turn_id": "turn-a",
                            "workspace": str(machine["workspace"]), "workspace_root": str(machine["workspace"]),
                            "surface": "openclaw", "platform": "openclaw"},
            hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
            checkpoint_id="runtime-checkpoint-1", step_index=0,
        )
    finally:
        reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)
    assert execution.mode == "tool_failed" and execution.status == "cwd_outside_workspace", (execution.mode, execution.status)
    assert not execution.details.get("approval_request")
    assert "Nothing was run" in execution.response_text


# --- the served chat, end to end -----------------------------------------------------------------


class _Recorder:
    """Stands in for the sandbox runner: records where the job would run."""

    jobs: list[tuple[str, str, str]] = []

    def __init__(self, _gate: Any, workspace_path: str, **_kwargs: Any) -> None:
        self.workspace = workspace_path

    def run_command(self, command: str, *, cwd: str, cancel_event: Any = None) -> dict[str, Any]:
        _Recorder.jobs.append((self.workspace, cwd, command))
        return {"status": "executed", "returncode": 0, "stdout": "-1\n", "stderr": ""}


def _served_turn(machine, tmp_path: Path, monkeypatch, *, text: str, call: dict[str, Any], mode: str) -> str:
    """One /api/chat turn as the window sends it (no workspace in the body), with the model's tool
    choice scripted: first `call`, then a closing reply."""
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core import runtime_execution_tools, runtime_paths
    from core.memory_first_router import ModelExecutionDecision
    from core.web.api.runtime import RuntimeServices
    from tests.asgi_harness import asgi_request

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(machine["workspace"]))
    _Recorder.jobs = []
    monkeypatch.setattr(runtime_execution_tools, "SandboxRunner", _Recorder)
    rounds = [0]

    def decide(**_kwargs: Any) -> ModelExecutionDecision:
        rounds[0] += 1
        output = call if rounds[0] == 1 else {"intent": "respond.direct", "arguments": {"message": "Done."}}
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"t{rounds[0]}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=output,
            confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )

    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    try:
        agent = VoolAgent(backend_name="test-backend", device="marked-code", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", agent)
        monkeypatch.setattr(agent.memory_router, "resolve_tool_intent", decide)
        app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
        body = {
            "messages": [{"role": "user", "content": text}],
            "session_id": f"openclaw:{uuid.uuid4().hex[:20]}",
            "stream": False,
            "mode": mode,
        }
        status, _headers, raw = asgi_request(
            app, method="POST", path="/api/chat",
            headers={"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"},
            body=json.dumps(body).encode("utf-8"),
        )
    finally:
        runtime_paths.configure_runtime_home(None)
    assert status == 200
    return str((json.loads(raw.decode("utf-8")).get("message") or {}).get("content") or "")


@pytest.mark.parametrize(
    ("text", "call"),
    [
        ("run calc.py in {folder}", {"intent": "sandbox.run_command", "arguments": {"command": "python calc.py", "cwd": "{folder}"}}),
        ("run the tests in {folder}", {"intent": "workspace.run_tests", "arguments": {"cwd": "{folder}"}}),
    ],
)
def test_the_served_chat_asks_to_approve_the_run_in_the_marked_folder(machine, tmp_path, monkeypatch, text, call) -> None:
    folder = str(machine["marked"])
    call = json.loads(json.dumps(call).replace("{folder}", folder.replace("\\", "\\\\")))
    reply = _served_turn(machine, tmp_path, monkeypatch, text=text.format(folder=folder), call=call, mode="manual")
    assert "requires approval" in reply and folder in reply, reply
    assert "outside the active workspace" not in reply and "open a chat bound" not in reply
    assert _Recorder.jobs == []  # nothing ran before the owner approved


@pytest.mark.parametrize(
    ("text", "intent", "arguments", "command"),
    [
        ("run calc.py in {folder}", "sandbox.run_command", {"command": "python calc.py"}, "python calc.py"),
        ("run the tests in {folder}", "workspace.run_tests", {"command": "python -m pytest -q"}, "python -m pytest -q"),
    ],
)
def test_the_served_chat_runs_the_job_in_the_marked_folder(machine, tmp_path, monkeypatch, text, intent, arguments, command) -> None:
    """Once allowed (Auto mode here), the sandbox job's folder and working directory are the marked
    folder (C3: run_tests was approved and then refused for its folder)."""
    folder = str(machine["marked"])
    call = {"intent": intent, "arguments": {**arguments, "cwd": folder}}
    reply = _served_turn(machine, tmp_path, monkeypatch, text=text.format(folder=folder), call=call, mode="auto")
    assert "outside the active workspace" not in reply, reply
    assert [(workspace, cwd) for workspace, cwd, _command in _Recorder.jobs] == [(folder, folder)]
    # On a Windows host run_tests names the interpreter by its full path
    # (C:\\...\\python.exe -m pytest -q): the program must still be Python, the arguments unchanged.
    program, _, rest = command.partition(" ")
    ran = _Recorder.jobs[0][2].strip()
    assert ran.endswith(f" {rest}"), (command, ran)
    ran_program = ran[: -len(rest) - 1].strip().strip('"')
    assert ntpath.basename(ran_program).lower() in {program, f"{program}.exe"}, (command, ran)
    assert list(machine["workspace"].iterdir()) == []


def test_the_served_chat_still_refuses_an_unmarked_folder(machine, tmp_path, monkeypatch) -> None:
    folder = str(machine["unmarked"])
    call = {"intent": "sandbox.run_command", "arguments": {"command": "python calc.py", "cwd": folder}}
    reply = _served_turn(machine, tmp_path, monkeypatch, text=f"run calc.py in {folder}", call=call, mode="auto")
    assert "requires approval" not in reply
    assert _Recorder.jobs == []


def test_the_served_chat_does_not_answer_run_calc_py_with_no_file(machine, tmp_path, monkeypatch) -> None:
    call = {"intent": "sandbox.run_command", "arguments": {"command": "python calc.py"}}
    reply = _served_turn(machine, tmp_path, monkeypatch, text="run calc.py", call=call, mode="manual")
    assert "There is no file at" not in reply, reply


def test_the_served_repair_task_opens_in_the_marked_folder(machine, tmp_path, monkeypatch) -> None:
    """C3: the code task a repair ask opens is placed in the marked folder, so its test runs and edits
    (`_code_task_folder` in the write gate) are checked there, not in the Desktop."""
    store = tmp_path / "code-tasks"
    monkeypatch.setenv("VOOL_CODE_TASK_DIR", str(store))
    folder = str(machine["marked"])
    call = {"intent": "code.task.open", "arguments": {"objective": "find why test_calc fails and fix it"}}
    _served_turn(machine, tmp_path, monkeypatch, mode="manual", call=call,
                 text=f"find why the test fails in {folder}, fix the cause, run the tests")
    journals = sorted(store.glob("ct-*.json"))
    assert journals
    assert {json.loads(path.read_text(encoding="utf-8"))["workspace_root"] for path in journals} == {folder}
