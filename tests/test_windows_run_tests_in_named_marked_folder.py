"""Windows: "run pytest / python -m unittest in <folder>" runs in the folder the user named.

Real hardware, M4 (VOOL 0.7.1, Windows 11). The owner marked `C:\\Users\\Satoshi\\VOOL HW Test`, whose
subfolder `proj` holds calc.py and a passing test_calc.py.

1. "run pytest in C:\\Users\\Satoshi\\VOOL HW Test\\proj", "run python -m pytest in <proj>" and the quoted
   path each showed the card "Run workspace.run_tests · Affects: -". After Allow: "Validation test run
   in `.`:", a failure, then "I wasn't able to turn that into a completed action."
2. "run python -m unittest in <proj>", quoted or not: "Run sandbox.run_command · Affects: -", and the
   command run was `python -m unittest in C:` (or `python -m unittest in`), exit 1,
   "ERROR: in (unittest.loader._FailedTest.in)".

The workflow planner reads "run <command>" from the typed text (`_explicit_command_request`) one
`[A-Za-z0-9_./:-]` word at a time. The " in <folder>" after the command was read as more of the
command, cut at the first `\\` (or at the opening quote), and no `cwd` was planned, so the job ran in the
chat's folder with `in C:` as arguments.

On Windows the " in <full path>" at the end of a run request is the job's working folder: read the way
"run calc.py in <folder>" reads its folder (`write_root_honesty`'s path reader, through folders with
spaces, quoted or not), and handed to the call as `cwd`, so the card names it and every gate still
decides it. A folder outside the marked folders is refused before any card, as before. Off Windows
the planner reads the request exactly as it did.

Windows is simulated as the other marked-folder tests do it: the write gate's `_IS_WINDOWS` (marked
folders from VOOL_WINDOWS_DISPOSABLE_WORKSPACES), the job runner's `_is_windows_platform` and the
command-line seam `network_guard._IS_WINDOWS`. The sandbox runner is replaced by a recorder that keeps
where each job would run and what it would run.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import mode_permission_policy as mpp
from core import windows_write_gate as gate
from sandbox import job_runner, network_guard

HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}
CALC = "def add(a, b):\n    return a + b\n\n\nprint(add(2, 3))\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

#: Each ask, with the call it plans and the command that call carries (no " in <folder>" left in it).
FORMS = {
    "pytest": ("workspace.run_tests", "pytest"),
    "pytest -q": ("workspace.run_tests", "pytest -q"),
    "python -m pytest": ("workspace.run_tests", "python -m pytest"),
    "python -m unittest": ("sandbox.run_command", "python -m unittest"),
}


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The chat's unmarked Desktop; two marked folders with spaces (one with `Ä`), each holding `proj`
    with calc.py and a passing test_calc.py; and an unmarked folder holding the same."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents"):
        (home / name).mkdir(parents=True)
    desktop = tmp_path / "OneDrive" / "Desktop"
    desktop.mkdir(parents=True)
    hw = home / "VOOL HW Test"
    umlaut = home / "VOOL Test" / "Allowed Ä folder"
    unmarked = home / "Other folder"
    for root in (hw, umlaut, unmarked):
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "calc.py").write_text(CALC, encoding="utf-8")
        (root / "proj" / "test_calc.py").write_text(TEST, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(hw), str(umlaut)]))
    return {"home": home, "desktop": desktop, "hw": hw, "umlaut": umlaut, "unmarked": unmarked}


@pytest.fixture
def windows_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job runner and the command line take their Windows branches."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)


class _Recorder:
    """Stands in for the sandbox runner: records where each job would run and what it would run."""

    jobs: list[dict[str, str]] = []

    def __init__(self, _gate: Any, workspace_path: str, **_kwargs: Any) -> None:
        self.workspace = workspace_path

    def run_command(self, command: str, *, cwd: str, cancel_event: Any = None) -> dict[str, Any]:
        _Recorder.jobs.append({"workspace": self.workspace, "cwd": cwd, "command": command})
        return {"status": "executed", "returncode": 0, "stdout": "1 passed\n", "stderr": "", "success": True}


class _Chat:
    """The VOOL window's chat against the served app: /api/chat, the card, `resolve_approval`, the resend.

    The model is never needed: if it is asked, it answers in prose, and nothing runs."""

    def __init__(self, machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, workspace: Path) -> None:
        from apps import vool_api_server
        from apps.vool_agent import VoolAgent
        from core import runtime_execution_tools, runtime_paths
        from core.memory_first_router import ModelExecutionDecision
        from core.web.api.runtime import RuntimeServices

        self.workspace = workspace
        self.desktop = machine["desktop"]
        monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
        monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(machine["desktop"]))
        runtime_paths.configure_runtime_home(tmp_path / "vool-home")
        mpp.reset_mode_permission_state()
        _Recorder.jobs = []
        monkeypatch.setattr(runtime_execution_tools, "SandboxRunner", _Recorder)
        self.session = f"openclaw:{uuid.uuid4().hex[:20]}"
        self.agent = VoolAgent(backend_name="test-backend", device="run-tests-folder", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", self.agent)

        def decide(**_kwargs: Any) -> ModelExecutionDecision:
            return ModelExecutionDecision(
                source="provider_execution", task_hash="t", provider_id="ollama-local:test",
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


def _typed(folder: Path, quote: str) -> str:
    return f"{quote}{folder}{quote}"


def _ask_allow_resume(talk: _Chat, text: str) -> tuple[dict[str, Any], str]:
    turn = f"turn-{uuid.uuid4().hex[:10]}"
    first = talk.say(text, turn_id=turn)
    pending = talk.pending()
    assert len(pending) == 1, first
    assert _Recorder.jobs == [], "nothing runs before the owner answers the card"
    card = dict(pending[0])
    assert mpp.resolve_approval(str(card["approval_id"]), decision="allow") is not None
    resumed = talk.say(text, turn_id=turn, token=str(card["approval_id"]))
    assert mpp._APPROVALS[str(card["approval_id"])]["status"] == "consumed", resumed
    return card, resumed


def _assert_ran_in(folder: Path, card: dict[str, Any], resumed: str, *, intent: str, command: str) -> None:
    assert card["intent"] == intent, card
    assert card["affected_resources"] == [str(folder)], card["affected_resources"]
    assert len(_Recorder.jobs) == 1, (_Recorder.jobs, resumed)
    job = _Recorder.jobs[0]
    assert os.path.samefile(job["cwd"], folder), job
    ran = job["command"]
    assert " in " not in f" {ran} " and "in C:" not in ran and str(folder) not in ran, ran
    # run_tests names the interpreter by its full path (`<python> -m pytest ...`): the rest is as asked.
    expected_tail = command.split(" ", 1)[1] if " " in command else ""
    program = command.split(" ", 1)[0]
    if program == "pytest":
        assert ran.endswith(f"-m pytest{(' ' + expected_tail) if expected_tail else ''}"), ran
    else:
        assert ran.endswith(expected_tail), ran
    assert "I wasn't able to turn that into a completed action" not in resumed, resumed
    assert "outside the active workspace" not in resumed, resumed


# --- 1/2. served: the named marked folder is the job's folder, and the card names it ----------------


@pytest.mark.parametrize("quote", ["", '"'], ids=["plain", "quoted"])
@pytest.mark.parametrize("which", ["hw", "umlaut"], ids=["spaces", "spaces-and-A-umlaut"])
@pytest.mark.parametrize("form", list(FORMS))
def test_an_unbound_chat_runs_the_ask_in_the_named_marked_folder(
    machine, chat, windows_jobs, form, which, quote
) -> None:
    folder = machine[which] / "proj"
    intent, command = FORMS[form]
    talk = chat(machine["desktop"])

    card, resumed = _ask_allow_resume(talk, f"run {form} in {_typed(folder, quote)}")

    _assert_ran_in(folder, card, resumed, intent=intent, command=command)
    assert list(machine["desktop"].iterdir()) == []


@pytest.mark.parametrize("quote", ["", '"'], ids=["plain", "quoted"])
@pytest.mark.parametrize("form", list(FORMS))
def test_a_chat_bound_to_the_marked_folder_runs_the_ask_in_its_named_subfolder(
    machine, chat, windows_jobs, form, quote
) -> None:
    """The chat works in the marked folder itself; the ask names its `proj` subfolder."""
    folder = machine["hw"] / "proj"
    intent, command = FORMS[form]
    talk = chat(machine["hw"])

    card, resumed = _ask_allow_resume(talk, f"run {form} in {_typed(folder, quote)}")

    _assert_ran_in(folder, card, resumed, intent=intent, command=command)


def test_run_python_calc_py_in_the_named_folder_keeps_running_there(machine, chat, windows_jobs) -> None:
    """The ask that already worked on hardware still runs in the folder, now without `in <folder>` as
    calc.py's arguments."""
    folder = machine["hw"] / "proj"
    talk = chat(machine["desktop"])

    card, resumed = _ask_allow_resume(talk, f"run python calc.py in {folder}")

    _assert_ran_in(folder, card, resumed, intent="sandbox.run_command", command="python calc.py")


# --- containment: a folder outside the marks is refused plainly, before any card --------------------


@pytest.mark.parametrize("quote", ["", '"'], ids=["plain", "quoted"])
@pytest.mark.parametrize("form", ["pytest", "python -m pytest", "python -m unittest"])
def test_a_named_folder_outside_the_marks_is_refused_before_any_card(machine, chat, windows_jobs, form, quote) -> None:
    folder = machine["unmarked"] / "proj"
    talk = chat(machine["desktop"])

    reply = talk.say(f"run {form} in {_typed(folder, quote)}", turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert talk.pending() == [], reply
    assert _Recorder.jobs == [], reply
    assert "requires approval" not in reply, reply
    assert "Nothing was run" in reply and str(folder) in reply, reply


def test_a_chat_bound_to_a_mark_naming_a_folder_outside_it_is_refused(machine, chat, windows_jobs) -> None:
    folder = machine["unmarked"] / "proj"
    talk = chat(machine["hw"])

    reply = talk.say(f"run python -m unittest in {folder}", turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert talk.pending() == [] and _Recorder.jobs == [], reply
    assert "Nothing was run" in reply and str(folder) in reply, reply


def test_a_named_folder_that_does_not_exist_inside_a_mark_is_refused(machine, chat, windows_jobs) -> None:
    folder = machine["hw"] / "missing"
    talk = chat(machine["desktop"])

    reply = talk.say(f"run pytest in {folder}", turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert talk.pending() == [] and _Recorder.jobs == [], reply
    assert "Nothing was run" in reply, reply


@pytest.mark.parametrize("bound", ["desktop", "hw"], ids=["unbound-chat", "chat-bound-to-the-mark"])
def test_a_junction_inside_a_mark_that_leads_out_is_refused_before_any_card(machine, chat, windows_jobs, bound) -> None:
    """A junction (a link here) inside the marked folder that leads to an unmarked folder: where it really
    leads is what counts, so "run pytest in <junction>" is refused plainly and nothing runs anywhere."""
    junction = machine["hw"] / "escape"
    junction.symlink_to(machine["unmarked"] / "proj", target_is_directory=True)
    talk = chat(machine[bound])

    reply = talk.say(f"run pytest in {junction}", turn_id=f"turn-{uuid.uuid4().hex[:10]}")

    assert talk.pending() == [], reply
    assert _Recorder.jobs == [], reply
    assert "requires approval" not in reply, reply
    assert "Nothing was run" in reply and str(junction) in reply, reply


@pytest.mark.parametrize("quote", ["", '"'], ids=["plain", "quoted"])
def test_a_folder_on_a_remote_drive_is_never_looked_up_and_nothing_runs_there(
    machine, chat, windows_jobs, monkeypatch, quote
) -> None:
    """`Z:` is a mapped network drive: looking `Z:\\proj` up is already the SMB connection. No stat, no
    realpath, no listing of it, from the ask through the card and the run, and no job runs in `Z:`."""
    from core import windows_known_folders
    from core.agent_runtime import write_root_honesty

    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive.upper() == "Z:")
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    looked: list[str] = []
    for name in ("isdir", "isfile", "exists", "lexists", "realpath"):
        real = getattr(os.path, name)

        def record(path, *args, _real=real, **kwargs):
            if "Z:" in str(path):
                looked.append(str(path))
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os.path, name, record)
    for name in ("stat", "lstat", "scandir", "listdir"):
        real_os = getattr(os, name)

        def record_os(path=".", *args, _real=real_os, **kwargs):
            if "Z:" in str(path):
                looked.append(str(path))
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, record_os)
    for name in ("is_dir", "is_file", "exists", "stat", "resolve", "iterdir"):
        real_method = getattr(Path, name)

        def record_method(self, *args, _real=real_method, **kwargs):
            if "Z:" in str(self):
                looked.append(str(self))
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, record_method)
    talk = chat(machine["desktop"])
    text = f"run pytest in {quote}Z:\\proj{quote}"
    turn = f"turn-{uuid.uuid4().hex[:10]}"

    reply = talk.say(text, turn_id=turn)
    for card in talk.pending():  # if a card is shown, Allow it: what then runs must still not be in Z:
        mpp.resolve_approval(str(card["approval_id"]), decision="allow")
        reply = talk.say(text, turn_id=turn, token=str(card["approval_id"]))
        assert "Z:" not in " ".join(str(item) for item in card.get("affected_resources") or []), card

    assert looked == [], (looked, reply)
    assert all("Z:" not in job["cwd"] for job in _Recorder.jobs), _Recorder.jobs


# --- the planner, read as Windows types it ------------------------------------------------------------

HW_TEXT = r"C:\Users\Satoshi\VOOL HW Test"
PROJ_TEXT = HW_TEXT + r"\proj"


def _plan(text: str, *, workspace: str) -> dict[str, Any]:
    from core.execution.planner import plan_tool_workflow

    decision = plan_tool_workflow(
        user_text=text, task_class="unknown", executed_steps=[], source_context={"workspace": workspace}
    )
    return dict(decision.next_payload or {})


@pytest.mark.parametrize("quote", ["", '"'], ids=["plain", "quoted"])
@pytest.mark.parametrize("form", list(FORMS))
def test_on_windows_the_in_folder_is_the_cwd_as_the_hardware_typed_it(monkeypatch, form, quote) -> None:
    """Steve's own spelling, backslashes and spaces, the mark read as text (no lookup). `C:` is a drive
    Windows calls local, as on the hardware, and the mark is listed as Windows lists it (`;` between
    entries, so on Linux the list is read with Windows' separator)."""
    from core import windows_known_folders

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False)
    monkeypatch.setattr(os, "pathsep", ";")
    monkeypatch.setenv(gate.ENV_VAR, HW_TEXT)
    intent, command = FORMS[form]

    payload = _plan(f"run {form} in {quote}{PROJ_TEXT}{quote}", workspace=HW_TEXT)

    assert payload == {"intent": intent, "arguments": {"command": command, "cwd": PROJ_TEXT}}, payload


# --- off Windows nothing changes -------------------------------------------------------------------

#: What the planner planned on base for each ask, and still plans off Windows.
OFF_WINDOWS = {
    ("pytest", ""): ("workspace.run_tests", "pytest in C:"),
    ("pytest", '"'): ("workspace.run_tests", "pytest in"),
    ("python -m pytest", ""): ("workspace.run_tests", "python -m pytest in C:"),
    ("python -m unittest", ""): ("sandbox.run_command", "python -m unittest in C:"),
    ("python -m unittest", '"'): ("sandbox.run_command", "python -m unittest in"),
}


@pytest.mark.parametrize(("form", "quote"), list(OFF_WINDOWS), ids=[f"{f}-{'quoted' if q else 'plain'}" for f, q in OFF_WINDOWS])
def test_off_windows_the_planner_reads_the_request_as_before(monkeypatch, form, quote) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.setenv(gate.ENV_VAR, HW_TEXT)
    intent, command = OFF_WINDOWS[(form, quote)]

    payload = _plan(f"run {form} in {quote}{PROJ_TEXT}{quote}", workspace=HW_TEXT)

    assert payload == {"intent": intent, "arguments": {"command": command}}, payload


def test_on_windows_a_folder_on_a_drive_windows_does_not_call_local_is_not_taken(monkeypatch) -> None:
    """A mapped network drive (or one Windows cannot name) is never looked up: the request is planned
    as on base, with no folder."""
    from core import windows_known_folders

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, HW_TEXT)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive.upper() == "Z:")

    payload = _plan(r"run python -m unittest in Z:\proj", workspace=HW_TEXT)

    assert payload == {"intent": "sandbox.run_command", "arguments": {"command": "python -m unittest in Z:"}}, payload


def test_off_windows_a_posix_folder_is_still_part_of_the_command(machine, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    folder = machine["hw"] / "proj"

    payload = _plan(f"run python -m unittest in {folder}", workspace=str(machine["hw"]))

    assert payload == {"intent": "sandbox.run_command", "arguments": {"command": f"python -m unittest in {folder}"}}


def test_off_windows_the_served_ask_runs_as_before(machine, chat, monkeypatch) -> None:
    """Served with the Windows seams off: the chat keeps its own folder and the command is read as on base."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    folder = machine["hw"] / "proj"
    talk = chat(machine["hw"])

    card, _resumed = _ask_allow_resume(talk, f"run python -m unittest in {folder}")

    assert card["intent"] == "sandbox.run_command" and card["affected_resources"] == [], card
    assert len(_Recorder.jobs) == 1, _Recorder.jobs
    assert os.path.samefile(_Recorder.jobs[0]["cwd"], machine["hw"]), _Recorder.jobs
    assert _Recorder.jobs[0]["command"] == f"python -m unittest in {folder}", _Recorder.jobs
