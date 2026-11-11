"""Windows: "run <command> in <marked folder>", served end to end, answers with what the command printed.

Real hardware, M4 round 3 (train 359aa71e, Windows 11, mark `C:\\Users\\Satoshi\\VOOL HW Test`, subfolder
`proj` holding calc.py and test_calc.py):

1. "run pytest in <proj>" and "run python -m pytest in <proj>": the card named proj, the job ran, and the
   reply was "I wasn't able to turn that into a completed action" (tool event "Validation test run in
   `.`:", routing model_tool_intent_command_failed). The job ran in proj: the turn works in the marked
   folder the ask names (`core.windows_marked_code_folder`), so proj IS the workspace and its relative
   name is `.`. The pytest run exited non-zero, and the tool loop answered a failed job with the generic
   failure sentence (`_tool_failure_user_message`), so what pytest printed never reached the owner.
2. "run python calc.py in <proj>" and "run python -m unittest in <proj>": the run exited 0, but the reply
   was the bounded builder lane's "I completed 1 bounded builder step under `generated/python-calc-...`",
   with no output: the planner plans the run as `sandbox.run_command`, and the builder lane claims every
   turn whose plan is one.

On Windows a whole-message run request is now answered with its job's own output: the builder lane does
not claim it, the tool loop ends on the job (exit 0 or not) with one written line and the job's text,
and the text names the folder in full when the turn works in the folder the ask named.

Driven through the served chat as the VOOL window sends it: /api/chat, the card, `resolve_approval`, the
same turn resent with its token, and the real executor (`_run_command`, `_run_validation`,
`SandboxRunner`, `JobRunner`'s phase-1 gate). Only the launch is replaced: VoolConfine cannot run here,
so the job runner runs the phase-1 argv (the interpreter running VOOL) as a plain child process in the
job's folder, on a temp marked folder holding a real calc.py and a real pytest / unittest test file.
Windows is simulated by the write gate's `_IS_WINDOWS` (marked folders from
VOOL_WINDOWS_DISPOSABLE_WORKSPACES) and the job runner's `_is_windows_platform`; the command line is read
by the host's own rules.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import mode_permission_policy as mpp
from core import windows_write_gate as gate
from sandbox import job_runner

HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
CALC = "def add(a, b):\n    return a + b\n\n\nif __name__ == '__main__':\n    print('calc ok', add(2, 3), add(4, 16))\n"
PASSING = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\n"
    "def test_add_with_pytest():\n    assert add(2, 3) == 5\n"
)
FAILING = PASSING.replace("add(2, 3), 5)", "add(2, 3), 6)").replace("add(2, 3) == 5", "add(2, 3) == 6")

#: Each ask, the call it plans, and what its output must show.
FORMS = {
    "python calc.py": ("sandbox.run_command", ["calc ok 5 20"]),
    "python -m unittest": ("sandbox.run_command", ["Ran 1 test", "OK"]),
    "pytest": ("workspace.run_tests", ["2 passed"]),
    "python -m pytest": ("workspace.run_tests", ["2 passed"]),
}


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The chat's unmarked Desktop, and the marked folder `VOOL HW Test` holding `proj` with calc.py and
    a passing test_calc.py (a unittest case and a pytest test)."""
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    desktop = tmp_path / "OneDrive" / "Desktop"
    desktop.mkdir(parents=True)
    hw = home / "VOOL HW Test"
    proj = hw / "proj"
    proj.mkdir(parents=True)
    (proj / "calc.py").write_text(CALC, encoding="utf-8")
    (proj / "test_calc.py").write_text(PASSING, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(hw))
    return {"home": home, "desktop": desktop, "hw": hw, "proj": proj}


class _Ran:
    def __init__(self, done: subprocess.CompletedProcess) -> None:
        self.returncode, self.stdout, self.stderr = done.returncode, done.stdout, done.stderr


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The job runner takes its Windows branch, and each job runs for real: the phase-1 argv
    (`JobRunner._windows_phase1_argv`, which raises for a refused program as the real run does) as a
    child process in the job's folder, in place of the VoolConfine launch."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    started: list[dict[str, Any]] = []
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}

    def run(self, argv, *, cwd=None, cancel_event=None, **_kwargs):
        launched = job_runner.JobRunner._windows_phase1_argv(list(argv))
        done = subprocess.run(
            launched, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", timeout=180, env=env
        )
        started.append({"argv": [str(item) for item in argv], "cwd": str(cwd), "returncode": done.returncode})
        return _Ran(done)

    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return started


class _Chat:
    """The VOOL window's chat against the served app. The model is never needed: asked, it answers in
    prose with no tool call, so a reply that holds the job's output came from the job."""

    def __init__(self, machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, workspace: Path) -> None:
        from apps import vool_api_server
        from apps.vool_agent import VoolAgent
        from core import runtime_paths
        from core.memory_first_router import ModelExecutionDecision
        from core.web.api.runtime import RuntimeServices

        self.workspace = workspace
        self.desktop = machine["desktop"]
        monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
        monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(machine["desktop"]))
        runtime_paths.configure_runtime_home(tmp_path / "vool-home")
        mpp.reset_mode_permission_state()
        self.session = f"openclaw:{uuid.uuid4().hex[:20]}"
        self.agent = VoolAgent(backend_name="test-backend", device="run-in-folder-e2e", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", self.agent)
        self.model_rounds = 0

        def decide(**_kwargs: Any) -> ModelExecutionDecision:
            self.model_rounds += 1
            return ModelExecutionDecision(
                source="provider_execution", task_hash=f"t{self.model_rounds}", provider_id="ollama-local:test",
                provider_name="ollama-local", model_name="test",
                structured_output={"intent": "respond.direct", "arguments": {"message": "Done."}},
                confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
            )

        monkeypatch.setattr(self.agent.memory_router, "resolve_tool_intent", decide)
        self.app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=self.agent))

    def say(self, text: str, *, turn_id: str, token: str = "") -> str:
        from tests.asgi_harness import asgi_request

        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}], "session_id": self.session, "stream": False,
            "mode": "manual", "turn_id": turn_id,
        }
        if self.workspace != self.desktop:
            body["workspace"] = str(self.workspace)  # a chat bound to its folder; an unbound chat sends none
        if token:
            body["approval_token"] = token
        status, _headers, raw = asgi_request(
            self.app, method="POST", path="/api/chat", headers=HEADERS, body=json.dumps(body).encode("utf-8")
        )
        assert status == 200, raw[:400]
        return str((json.loads(raw.decode("utf-8") or "{}").get("message") or {}).get("content") or "")

    def pending(self) -> list[dict[str, Any]]:
        return [a for a in mpp._APPROVALS.values() if a.get("status") == "pending"]

    def ask_allow_resume(self, text: str) -> tuple[dict[str, Any], str]:
        turn = f"turn-{uuid.uuid4().hex[:10]}"
        first = self.say(text, turn_id=turn)
        pending = self.pending()
        assert len(pending) == 1, first
        card = dict(pending[0])
        assert mpp.resolve_approval(str(card["approval_id"]), decision="allow") is not None
        resumed = self.say(text, turn_id=turn, token=str(card["approval_id"]))
        assert mpp._APPROVALS[str(card["approval_id"])]["status"] == "consumed", resumed
        return card, resumed


@pytest.fixture
def chat(machine, tmp_path, monkeypatch):
    from core import runtime_paths

    def open_chat(workspace: Path) -> _Chat:
        return _Chat(machine, tmp_path, monkeypatch, workspace=workspace)

    try:
        yield open_chat
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


def _assert_not_the_builder_lane(machine: dict[str, Path], reply: str) -> None:
    assert "bounded builder step" not in reply, reply
    assert "generated/" not in reply.replace("\\", "/"), reply
    assert not (machine["proj"] / "generated").exists() and not (machine["hw"] / "generated").exists()


# --- served end to end: the job runs in the named folder and the reply is its output ------------------


@pytest.mark.parametrize(
    ("binding", "quote"),
    [("unbound", ""), ("unbound", '"'), ("bound-to-the-mark", "")],
    ids=["unbound-plain", "unbound-quoted", "bound-to-the-mark"],
)
@pytest.mark.parametrize("form", list(FORMS))
def test_the_run_in_the_named_marked_folder_answers_with_its_output(
    machine, chat, jobs, form, binding, quote
) -> None:
    folder = machine["proj"]
    intent, shown = FORMS[form]
    talk = chat(machine["desktop"] if binding == "unbound" else machine["hw"])

    card, reply = talk.ask_allow_resume(f"run {form} in {quote}{folder}{quote}")

    assert card["intent"] == intent and card["affected_resources"] == [str(folder)], card
    assert len(jobs) == 1, (jobs, reply)
    assert os.path.samefile(jobs[0]["cwd"], folder), jobs
    assert jobs[0]["returncode"] == 0, (jobs, reply)
    assert "ran and exited 0." in reply, reply
    for text in shown:
        assert text in reply, (text, reply)
    # The reply names the folder the job ran in: in full when the turn works in it, by its name in the mark.
    assert (f"in `{folder}`:" if binding == "unbound" else "in `proj`:") in reply, reply
    assert "I wasn't able to turn that into a completed action" not in reply, reply
    assert "model synthesis failed" not in reply, reply
    _assert_not_the_builder_lane(machine, reply)


@pytest.mark.parametrize(
    ("form", "shown"),
    [("pytest", "test_calc.py FF"), ("python -m pytest", "test_calc.py FF"), ("python -m unittest", "FAILED (failures=1)")],
)
def test_a_failing_test_run_shows_what_failed(machine, chat, jobs, form, shown) -> None:
    """The hardware's pytest case: the run exits non-zero. The reply says so and shows the output, never
    the generic "I wasn't able to turn that into a completed action"."""
    (machine["proj"] / "test_calc.py").write_text(FAILING, encoding="utf-8")
    talk = chat(machine["desktop"])

    _card, reply = talk.ask_allow_resume(f"run {form} in {machine['proj']}")

    assert len(jobs) == 1 and jobs[0]["returncode"] == 1, (jobs, reply)
    assert os.path.samefile(jobs[0]["cwd"], machine["proj"]), jobs
    assert "ran and failed (exit code 1)." in reply, reply
    assert shown in reply and "assert" in reply.lower(), reply
    assert "I wasn't able to turn that into a completed action" not in reply, reply
    _assert_not_the_builder_lane(machine, reply)


def test_a_run_with_no_folder_in_a_chat_bound_to_it_answers_with_its_output(machine, chat, jobs) -> None:
    """"run python calc.py" in a chat bound to proj: the same answer, with no folder in the ask."""
    talk = chat(machine["proj"])

    _card, reply = talk.ask_allow_resume("run python calc.py")

    assert len(jobs) == 1 and os.path.samefile(jobs[0]["cwd"], machine["proj"]), jobs
    assert "ran and exited 0." in reply and "calc ok 5 20" in reply, reply
    _assert_not_the_builder_lane(machine, reply)


# --- containment is unchanged ---------------------------------------------------------------------------


def test_an_unmarked_folder_is_still_refused_before_any_card(machine, chat, jobs) -> None:
    other = machine["home"] / "Other folder"
    other.mkdir()
    (other / "calc.py").write_text(CALC, encoding="utf-8")
    talk = chat(machine["desktop"])

    reply = talk.say(f"run python calc.py in {other}", turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert talk.pending() == [] and jobs == [], reply
    assert "Nothing was run" in reply and str(other) in reply, reply


# --- off Windows nothing changes ---------------------------------------------------------------------------


def test_off_windows_the_run_request_helpers_say_nothing(monkeypatch) -> None:
    from core.agent_runtime.research_tool_loop_facade import (
        is_windows_run_request_answer,
        windows_run_request_answer,
        windows_run_request_step,
    )
    from core.agent_runtime.windows_program_request import windows_run_request_command

    step = {
        "tool_name": "sandbox.run_command", "status": "command_failed", "mode": "tool_failed",
        "details": {"command": "python calc.py", "returncode": 1},
        "response_text": 'Command failed in `proj`:\n$ python calc.py\n- Exit code: 1\n- Stderr:\nTraceback (most recent call last):\n  File "calc.py", line 1',
    }
    answer = windows_run_request_answer(step)
    assert answer.startswith("`python calc.py` ran and failed (exit code 1).\n\nCommand failed in `proj`:"), answer
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert windows_run_request_command("run python calc.py") == "python calc.py"
    assert windows_run_request_step("run python calc.py", [step]) is step
    assert is_windows_run_request_answer(answer)
    assert not is_windows_run_request_answer('Traceback (most recent call last):\n  File "x.py", line 1')
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert windows_run_request_command("run python calc.py") == ""
    assert windows_run_request_step("run python calc.py", [step]) is None
    assert not is_windows_run_request_answer(answer)


class _Recorder:
    """Off Windows the job runner is the host's: a recorder stands in for the sandbox runner."""

    jobs: list[tuple[str, str]] = []

    def __init__(self, _gate: Any, workspace_path: str, **_kwargs: Any) -> None:
        self.workspace = workspace_path

    def run_command(self, command: str, *, cwd: str, cancel_event: Any = None) -> dict[str, Any]:
        _Recorder.jobs.append((cwd, command))
        return {"status": "executed", "returncode": 0, "stdout": "calc ok 5 20\n", "stderr": "", "success": True}


def test_off_windows_a_run_request_is_still_the_builder_lanes(machine, chat, monkeypatch) -> None:
    """Mac/Linux: "run python calc.py" in a chat bound to its folder is served as on base, by the bounded
    builder lane, and its job line is the relative folder."""
    from core import runtime_execution_tools

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    _Recorder.jobs = []
    monkeypatch.setattr(runtime_execution_tools, "SandboxRunner", _Recorder)
    talk = chat(machine["proj"])

    _card, reply = talk.ask_allow_resume("run python calc.py")

    assert len(_Recorder.jobs) == 1, _Recorder.jobs
    assert "bounded builder step" in reply, reply
    assert "ran and exited 0." not in reply, reply


def test_off_windows_the_job_line_keeps_its_relative_folder(machine, monkeypatch) -> None:
    from core.runtime_execution_tools import _windows_named_folder_label

    context = {"workspace_binding": "marked_folder"}
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert _windows_named_folder_label(machine["proj"], ".", context) == "."
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    assert _windows_named_folder_label(machine["proj"], ".", context) == str(machine["proj"])
    assert _windows_named_folder_label(machine["proj"], ".", {}) == "."
    assert _windows_named_folder_label(machine["proj"], "proj", context) == "proj"
