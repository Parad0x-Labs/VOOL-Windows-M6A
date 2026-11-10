"""Windows captest round 2 (build a22771c2, clean Windows 11 VM, marked folders): two asks.

1. A write or a move that used `..` to reach outside a marked folder ("move x.txt to ..\\y.txt", "write
   hi to ..\\y.txt", asked in a marked folder) was answered "There is no file at `y.txt`", not with the
   plain refusal every other ask outside the marked folders gets.
   - The turn splitter (`answer_coverage._is_boundary`) read the first `.` of a `..` name as the end of
     a sentence. "write hi to <marked>\\..\\y.txt" became the two requests "write hi to <marked>\\." and
     "\\y.txt", and the second was served on its own as a read of `y.txt` in the chat's folder.
   - A climb typed relative to the chat's folder ("..\\y.txt", "in ..") was never checked: the
     write-root check reads only full paths, and the lanes place a relative path from somewhere else
     (the machine writer from the home, the move tool from VOOL's working folder) or drop the `..`.
2. C1: "run calc.py" in a marked folder showed the approval card; after Allow, "Resuming pending tool..."
   and nothing ran. "python calc.py" runs once after one Allow since 5c2ba8f3. The same ask planned as
   `py calc.py` (the Windows launcher's name) passed the check before approval, was approved and spent,
   and was then refused by the sandbox's own program list, which never held `py`, `py.exe` or `py.test`
   although phase 1 runs them as the interpreter running VOOL.

Windows is simulated as the other Windows tests do it: the write gate's `_IS_WINDOWS` (marked folders
from VOOL_WINDOWS_DISPOSABLE_WORKSPACES), the job runner's `_is_windows_platform` and the command-line
seam `network_guard._IS_WINDOWS`, and `write_root_honesty._path_separators` for `\\` as a separator. The
job runner is replaced by a recorder that keeps each job's argv and folder and the argv phase 1 would
hand the launcher (`JobRunner._windows_phase1_argv`, which raises for a refused form as the real run
does). The chat is served as the VOOL window sends it: streamed /api/chat, then /api/mode
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
from core import windows_write_gate as gate
from core.agent_runtime import write_root_honesty
from sandbox import job_runner, network_guard

SPACED = r"C:\Users\VOOLT\VOOL Test\Allowed Ä folder"
HEADERS = {"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"}


# --- the machine -----------------------------------------------------------------------------------


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home with Desktop and Documents, an unmarked Desktop workspace, and a marked folder with spaces
    and `Ä` inside the home, holding x.txt and calc.py, as on the VM."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents"):
        (home / name).mkdir(parents=True)
    desktop = tmp_path / "OneDrive" / "Desktop"
    desktop.mkdir(parents=True)
    marked = home / "VOOL Test" / "Allowed Ä folder"
    marked.mkdir(parents=True)
    (marked / "x.txt").write_text("x\n", encoding="utf-8")
    (marked / "calc.py").write_text("print(2 + 3)\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    return {"root": tmp_path, "home": home, "desktop": desktop, "marked": marked}


@pytest.fixture
def backslash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`\\` is a path separator, as on Windows."""
    monkeypatch.setattr(write_root_honesty, "_path_separators", lambda: "\\/")


def _files(machine: dict[str, Path]) -> dict[str, bytes]:
    """Every file under the home and the Desktop workspace, with its bytes."""
    found: dict[str, bytes] = {}
    for base in (machine["home"], machine["desktop"]):
        for path in sorted(base.rglob("*")):
            if path.is_file():
                found[str(path)] = path.read_bytes()
    return found


class _Chat:
    """The VOOL window's chat against the served app: streamed /api/chat, /api/mode, the resend."""

    def __init__(self, machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, workspace: Path,
                 calls: list[dict[str, Any]] | None = None, jobs: list | None = None) -> None:
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
        self.calls = list(calls or [])
        self.jobs = jobs if jobs is not None else []
        self.rounds = 0
        self.session = f"openclaw:{uuid.uuid4().hex[:20]}"
        self.agent = VoolAgent(backend_name="test-backend", device="dotdot-run-script", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", self.agent)
        if self.calls:
            def decide(**_kwargs: Any) -> ModelExecutionDecision:
                # Like the model: it asks for the scripted call until a job has run, then it answers.
                self.rounds += 1
                output = self.calls[0] if not self.jobs else {"intent": "respond.direct", "arguments": {"message": "Done."}}
                return ModelExecutionDecision(
                    source="provider_execution", task_hash=f"t{self.rounds}", provider_id="ollama-local:test",
                    provider_name="ollama-local", model_name="test", structured_output=output,
                    confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
                )

            monkeypatch.setattr(self.agent.memory_router, "resolve_tool_intent", decide)
        self.runtime = RuntimeServices(display_name="VOOL", agent=self.agent)
        self.app = vool_api_server.create_app(self.runtime)

    def say(self, text: str, *, mode: str = "manual", turn_id: str = "", token: str = "", stream: bool = False) -> str:
        """One turn. Not streamed: through the served app from the loopback peer, as the owner's own turn.
        Streamed: as the window streams it (`_dispatch_post`), for a chat bound to its folder."""
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}], "session_id": self.session, "stream": stream, "mode": mode,
        }
        if self.workspace != self.desktop:
            body["workspace"] = str(self.workspace)  # a chat bound to its folder; an unbound chat sends none
        if turn_id:
            body["turn_id"] = turn_id
        if token:
            body["approval_token"] = token
        if stream:
            from apps.vool_api_server import _dispatch_post

            response = _dispatch_post(
                path="/api/chat", body=body, headers={"content-type": "application/json"}, runtime=self.runtime,
                model_name="vool", workspace_root_provider=lambda: str(self.desktop),
            )
            raw = b"".join(response.stream or ())
            assert response.status == 200
            frames = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            return "".join(str((f.get("message") or {}).get("content") or "") for f in frames if isinstance(f, dict))
        from tests.asgi_harness import asgi_request

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

    made: list[_Chat] = []

    def open_chat(**kwargs: Any) -> _Chat:
        made.append(_Chat(machine, tmp_path, monkeypatch, **kwargs))
        return made[-1]

    try:
        yield open_chat
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


# --- 1. the turn splitter keeps a path's `..` and `.` names --------------------------------------


@pytest.fixture
def windows_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows seam the splitter reads (the write gate's): on."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


#: The VM's write, and how the splitter cut it on base (and still cuts it off Windows).
VM_WRITE = rf"write hi to {SPACED}\..\y.txt"
VM_WRITE_OLD_SPLIT = [rf"write hi to {SPACED}\.", r"\y.txt"]


def test_off_windows_the_splitter_is_unchanged_and_on_windows_it_keeps_the_path(monkeypatch) -> None:
    """Security Architect condition on dbceb2f5: on Mac and Linux sentence splitting stays byte for byte as
    it was; only the Windows seam keeps a path's `..` inside the request. Asked off, on, then off again for
    the same text, so a cached reading of one seam is never served for the other."""
    from core.agent_runtime.answer_coverage import interpret_request, turn_slices
    from core.agent_runtime.demand_ownership import execution_units

    readings = {}
    for seam in (False, True, False):
        monkeypatch.setattr(gate, "_IS_WINDOWS", seam)
        readings.setdefault(seam, []).append((
            [part.text for part in turn_slices(VM_WRITE)],
            [unit.text for unit in interpret_request(VM_WRITE).requests],
            [unit for _id, unit in execution_units(VM_WRITE)],
        ))
    off_first, off_again = readings[False]
    assert off_first == off_again == (VM_WRITE_OLD_SPLIT, VM_WRITE_OLD_SPLIT, VM_WRITE_OLD_SPLIT), off_first
    assert readings[True] == [([VM_WRITE], [VM_WRITE], [VM_WRITE])], readings[True]
    for text, old in (("move x.txt to ../y.txt", ["move x.txt to .", "/y.txt"]),
                      ("create y.txt in .. with the text hi", ["create y.txt in .", "with the text hi"])):
        monkeypatch.setattr(gate, "_IS_WINDOWS", False)
        assert [part.text for part in turn_slices(text)] == old, text
        monkeypatch.setattr(gate, "_IS_WINDOWS", True)
        assert [part.text for part in turn_slices(text)] == [text], text



@pytest.mark.parametrize(
    "text",
    [
        rf"write hi to {SPACED}\..\y.txt",
        rf"create {SPACED}\..\y.txt with the text hi",
        rf"move {SPACED}\x.txt to {SPACED}\..\y.txt",
        "write hi to C:/Users/VOOLT/VOOL Test/Allowed Ä folder/../y.txt",
        r"move x.txt to ..\y.txt",
        r"write hi to ..\y.txt",
        "write hi to ../y.txt",
        "create y.txt in .. with the text hi",
        rf"create escape.txt in {SPACED}\.. with the text hi",
        r"run .\calc.py",
    ],
)
def test_a_path_dot_name_does_not_end_the_request(windows_seam, text: str) -> None:
    from core.agent_runtime.answer_coverage import turn_slices
    from core.agent_runtime.demand_ownership import execution_units, units_as_plan

    assert [part.text for part in turn_slices(text)] == [text]
    assert [unit for _id, unit in execution_units(text)] == [text]
    assert [task.request for task in units_as_plan(text)] == [text]


def test_the_vm_write_is_no_longer_split_into_a_read_of_y_txt(windows_seam) -> None:
    """What base served: the second part, `\\y.txt`, is read by the workspace read lane as `y.txt`."""
    from core.agent_runtime.demand_ownership import execution_units

    units = [unit for _id, unit in execution_units(rf"write hi to {SPACED}\..\y.txt")]
    assert r"\y.txt" not in units and len(units) == 1, units


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("Hello. Tell me a joke.", ["Hello.", "Tell me a joke."]),
        ("Thanks... What is the weather in Vilnius?", ["Thanks.", "What is the weather in Vilnius?"]),
        ("Done. Now read notes.txt.", ["Done.", "Now read notes.txt."]),
        ("wait.. what? ok", ["wait.", "what?", "ok"]),
    ],
)
@pytest.mark.parametrize("seam", [False, True], ids=["off-windows", "windows"])
def test_a_sentence_still_ends_at_its_full_stop(monkeypatch, text: str, parts: list[str], seam: bool) -> None:
    """Guard (passes on base too): a full stop, an ellipsis and a `..` after a word still end a sentence."""
    from core.agent_runtime.answer_coverage import turn_slices

    monkeypatch.setattr(gate, "_IS_WINDOWS", seam)

    assert [part.text for part in turn_slices(text)] == parts


# --- 1. served: a climb out of a marked folder is refused before approval, nothing touched --------


@pytest.mark.parametrize(
    "form",
    [r"write hi to {marked}\..\y.txt", r"create {marked}\..\y.txt with the text hi"],
    ids=["write-to", "create-with-text"],
)
def test_a_full_path_climb_with_backslashes_is_refused_not_read(machine, chat, form: str) -> None:
    """The VM's spelling from an unbound chat (its Desktop): the full path, `\\` before the `..`."""
    before = _files(machine)
    reply = chat(workspace=machine["desktop"]).say(form.format(marked=machine["marked"]))
    assert "There is no file at" not in reply, reply
    assert "I can't write to" in reply and "Nothing was written on this turn." in reply, reply
    assert mpp._APPROVALS == {} or not [a for a in mpp._APPROVALS.values() if a.get("status") == "pending"]
    assert _files(machine) == before


@pytest.mark.parametrize("separator", ["/", "\\"], ids=["slash", "backslash"])
def test_a_full_path_move_that_climbs_out_is_the_move_lanes_refusal(machine, chat, separator) -> None:
    """Guard (passes on base too): the marked-folder move lane refuses it, naming both paths."""
    before = _files(machine)
    marked = machine["marked"]
    destination = f"{marked}{separator}..{separator}y.txt"
    reply = chat(workspace=machine["desktop"]).say(f"move {marked / 'x.txt'} to {destination}")
    assert reply == (
        f"`{destination}` is outside the folders you marked in Settings, Folders VOOL may change (where it really "
        f"leads, links and junctions followed, is what counts), so I won't move `{marked / 'x.txt'}` there. "
        "Nothing was moved."
    ), reply
    assert _files(machine) == before


@pytest.mark.parametrize("separator", ["/", "\\"], ids=["slash", "backslash"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_relative_write_that_climbs_out_of_the_marked_folder_is_refused(machine, chat, backslash, separator, mode) -> None:
    """Asked in the marked folder: "write hi to ..\\y.txt". The write-root sentence, naming where it leads."""
    before = _files(machine)
    typed = f"..{separator}y.txt"
    talk = chat(workspace=machine["marked"])
    reply = talk.say(f"write hi to {typed}", mode=mode)
    leads_to = machine["marked"].parent / "y.txt"
    assert reply.startswith(f"I can't write to `{typed}`."), reply
    assert f"Its `..` climbs out of this chat's folder, so it leads to `{leads_to}`" in reply, reply
    assert "outside every folder you marked" in reply and "Nothing was written on this turn." in reply, reply
    assert "There is no file at" not in reply
    assert talk.pending() == [] and _files(machine) == before


@pytest.mark.parametrize("separator", ["/", "\\"], ids=["slash", "backslash"])
def test_a_relative_move_that_climbs_out_of_the_marked_folder_is_refused(machine, chat, backslash, separator) -> None:
    """Asked in the marked folder: "move x.txt to ..\\y.txt". The move lane's sentence, naming where it leads."""
    before = _files(machine)
    typed = f"..{separator}y.txt"
    talk = chat(workspace=machine["marked"])
    reply = talk.say(f"move x.txt to {typed}")
    leads_to = machine["marked"].parent / "y.txt"
    assert reply == (
        f"I can't move that: `{typed}` (that is `{leads_to}`) is outside the folders you marked as ones VOOL "
        f"may change ({gate.ENV_VAR}), and on Windows VOOL moves files only inside them. Nothing was moved."
    ), reply
    assert talk.pending() == [] and _files(machine) == before


def test_a_bare_climb_as_the_folder_is_not_written_into_the_marked_folder(machine, chat) -> None:
    """"create y.txt in .. with the text hi" in the marked folder: base wrote `<marked>/y.txt` in Auto mode
    (the `..` dropped) and asked to approve that write in Manual mode."""
    before = _files(machine)
    for mode in ("manual", "auto"):
        talk = chat(workspace=machine["marked"])
        reply = talk.say("create y.txt in .. with the text hi", mode=mode)
        assert reply.startswith("I can't write to `..`."), (mode, reply)
        assert "Nothing was written on this turn." in reply
        assert talk.pending() == [], mode
    assert _files(machine) == before
    assert not (machine["marked"] / "y.txt").exists()


def test_streamed_as_the_window_sends_it_a_relative_climb_is_refused(machine, chat, backslash) -> None:
    before = _files(machine)
    talk = chat(workspace=machine["marked"])
    for text, opening in ((r"write hi to ..\y.txt", "I can't write to `..\\y.txt`."),
                          (r"move x.txt to ..\y.txt", "I can't move that: `..\\y.txt`")):
        reply = talk.say(text, stream=True)
        assert reply.startswith(opening), (text, reply)
        assert "There is no file at" not in reply
    assert talk.pending() == [] and _files(machine) == before


# --- 1. the relative climb check itself ----------------------------------------------------------


def test_a_climb_that_stays_inside_a_marked_folder_is_left_to_the_lanes(machine) -> None:
    sub = machine["marked"] / "sub"
    sub.mkdir()
    context = {"workspace": str(sub)}
    assert write_root_honesty.climbing_relative_target("write hi to ../y.txt", source_context=context) == ("", "", "")
    kind, path, leads_to = write_root_honesty.climbing_relative_target("write hi to ../../y.txt", source_context=context)
    assert (kind, path, leads_to) == ("write", "../../y.txt", str(machine["marked"].parent / "y.txt"))


def test_a_read_or_a_plain_mention_of_a_climb_is_not_refused(machine) -> None:
    context = {"workspace": str(machine["marked"])}
    for text in ("read ../y.txt", "what is in ../notes.txt", "the file sits at ../y.txt, I think",
                 "save https://example.com/a/../b.txt"):
        assert write_root_honesty.climbing_relative_target(text, source_context=context) == ("", "", ""), text


def test_off_windows_a_relative_climb_is_not_judged_here(machine, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    context = {"workspace": str(machine["marked"])}
    assert write_root_honesty.climbing_relative_target("write hi to ../y.txt", source_context=context) == ("", "", "")


# --- 2. "run calc.py": an approved Python run runs once, as approved -------------------------------


class _Ran:
    def __init__(self, stdout: str) -> None:
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    started: list[dict[str, Any]] = []

    def run(self, argv, *, cwd=None, cancel_event=None, **_kwargs):
        record: dict[str, Any] = {"argv": [str(item) for item in argv], "cwd": str(cwd or "")}
        started.append(record)
        if job_runner._is_windows_platform():
            record["launched"] = job_runner.JobRunner._windows_phase1_argv(list(argv))
        return _Ran("5\n")

    monkeypatch.setattr(job_runner.JobRunner, "run", run)
    return started


@pytest.fixture
def windows_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The job runner and the command line take their Windows branches; PATH holds the Store alias only."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    store = tmp_path / "WindowsApps"
    store.mkdir()
    for name in ("python.exe", "python3.exe", "py.exe"):
        (store / name).write_bytes(b"")
    monkeypatch.setenv("PATH", str(store))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")


def _interpreter() -> str:
    return str(Path(sys.executable).resolve())


#: The calls a model may plan for "run calc.py", with what phase 1 hands the launcher after the interpreter.
RUN_CALC = {
    "python calc.py": ["calc.py"],
    "py calc.py": ["calc.py"],
    "py.exe calc.py": ["calc.py"],
    "python3 calc.py": ["calc.py"],
    r"python .\calc.py": [r".\calc.py"],
}


def _ask_allow_resume(talk: _Chat, text: str, *, decision: str = "allow") -> tuple[str, str, str]:
    turn = f"turn-{uuid.uuid4().hex[:10]}"
    first = talk.say(text, turn_id=turn)
    pending = talk.pending()
    assert len(pending) == 1, first
    assert talk.jobs == [], "nothing runs before the owner answers the card"
    token = str(pending[0]["approval_id"])
    assert mpp.resolve_approval(token, decision=decision) is not None
    resumed = talk.say(text, turn_id=turn, token=token)
    return first, resumed, token


@pytest.mark.parametrize("command", list(RUN_CALC))
def test_run_calc_py_in_the_marked_folder_runs_once_as_approved(machine, chat, windows_sandbox, jobs, command) -> None:
    call = {"intent": "sandbox.run_command", "arguments": {"command": command}}
    talk = chat(workspace=machine["marked"], calls=[call], jobs=jobs)

    _first, resumed, token = _ask_allow_resume(talk, "run calc.py")

    assert "wasn't able" not in resumed and "not in Sandbox whitelist" not in resumed, resumed
    assert len(jobs) == 1, (jobs, resumed)
    assert jobs[0]["argv"] == network_guard.parse_command(command), jobs
    assert jobs[0]["launched"] == [_interpreter(), *RUN_CALC[command]], jobs
    assert os.path.samefile(jobs[0]["cwd"], machine["marked"]), jobs
    assert mpp._APPROVALS[token]["status"] == "consumed"


def test_streamed_as_the_window_sends_it_an_approved_py_run_runs_once(machine, chat, windows_sandbox, jobs) -> None:
    """The window streams the ask and the resend; the resumed call must run, not end on "Resuming pending tool"."""
    talk = chat(workspace=machine["marked"], calls=[{"intent": "sandbox.run_command", "arguments": {"command": "py calc.py"}}], jobs=jobs)
    turn = f"turn-{uuid.uuid4().hex[:10]}"
    talk.say("run calc.py", turn_id=turn, stream=True)
    pending = talk.pending()
    assert len(pending) == 1 and jobs == []
    token = str(pending[0]["approval_id"])
    mpp.resolve_approval(token, decision="allow")
    resumed = talk.say("run calc.py", turn_id=turn, token=token, stream=True)
    assert "wasn't able" not in resumed, resumed
    assert [job["launched"] for job in jobs] == [[_interpreter(), "calc.py"]]
    assert mpp._APPROVALS[token]["status"] == "consumed"


def test_run_calc_py_named_in_the_marked_folder_from_the_desktop_runs_there(machine, chat, windows_sandbox, jobs) -> None:
    call = {"intent": "sandbox.run_command", "arguments": {"command": "py calc.py"}}
    talk = chat(workspace=machine["desktop"], calls=[call], jobs=jobs)

    _first, resumed, token = _ask_allow_resume(talk, f"run calc.py in {machine['marked']}")

    assert len(jobs) == 1, (jobs, resumed)
    assert jobs[0]["launched"] == [_interpreter(), "calc.py"], jobs
    assert os.path.samefile(jobs[0]["cwd"], machine["marked"]), jobs
    assert mpp._APPROVALS[token]["status"] == "consumed"


@pytest.mark.parametrize("command", ["python calc.py", "py calc.py"])
def test_a_denied_card_runs_nothing(machine, chat, windows_sandbox, jobs, command) -> None:
    """Guard: DENY stays DENY."""
    call = {"intent": "sandbox.run_command", "arguments": {"command": command}}
    talk = chat(workspace=machine["marked"], calls=[call], jobs=jobs)

    _first, _resumed, token = _ask_allow_resume(talk, "run calc.py", decision="deny")

    assert jobs == []
    assert mpp._APPROVALS[token]["status"] != "consumed"


def _executor(workspace: Path, session: str, turn: str, mode: str = "manual"):
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
    from core.tool_intent_executor import execute_tool_intent

    context = {"runtime_session_id": session, "operating_mode": mode, "cancel_turn_id": turn,
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
def executor(tmp_path, monkeypatch, machine):
    from core import runtime_paths

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    mpp.reset_mode_permission_state()
    session, turn = f"openclaw:{uuid.uuid4().hex[:12]}", "turn-a"
    mpp.set_active_mode(session, "manual", client_turn_id=turn)
    try:
        yield _executor(machine["marked"], session, turn)
    finally:
        mpp.reset_mode_permission_state()
        runtime_paths.configure_runtime_home(None)


@pytest.mark.parametrize(
    ("command", "launched"),
    [("py calc.py", ["calc.py"]), ("py.test -q", ["-m", "pytest", "-q"]), ("py -m unittest", ["-m", "unittest"])],
)
def test_one_allow_runs_an_approved_py_command_once(windows_sandbox, jobs, executor, machine, command, launched) -> None:
    approved = {"command": command, "cwd": str(machine["marked"])}
    asked = executor(approved)
    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    token = str(asked.details["approval_request"]["approval_id"])
    assert mpp.resolve_approval(token, decision="allow") is not None

    first = executor(approved, token)
    replay = executor(approved, token)

    assert first.status == "executed", (first.status, first.response_text)
    assert replay.status != "executed"
    assert [job["launched"] for job in jobs] == [[_interpreter(), *launched]]


def test_the_py_token_never_runs_another_argv(windows_sandbox, jobs, executor, machine) -> None:
    """Guard: the approval covers exactly its own call."""
    approved = {"command": "py calc.py", "cwd": str(machine["marked"])}
    asked = executor(approved)
    token = str(asked.details["approval_request"]["approval_id"])
    mpp.resolve_approval(token, decision="allow")
    other = executor({**approved, "command": "py other.py"}, token)
    assert other.status == "pending_approval" and jobs == [], (other.status, jobs)
    assert mpp._APPROVALS[token]["status"] == "approved"


def test_a_py_form_phase_1_refuses_is_refused_before_the_card(windows_sandbox, jobs, executor, machine) -> None:
    """Guard: `py -3 calc.py` and `py -m pip install x` are refused before any approval, as on base."""
    for command in ("py -3 calc.py", "py -m pip install requests"):
        refused = executor({"command": command, "cwd": str(machine["marked"])})
        assert refused.status == "windows_program_refused" and not refused.details.get("approval_request"), command
    assert jobs == []


def test_a_mode_that_denies_the_command_still_denies_py_under_an_approved_scope(windows_sandbox, machine) -> None:
    """Guard: an approval lifts only an ask; Plan mode's DENY stands for `py` too."""
    from core.effect_gateway import named_background_effect_scope

    cwd = str(machine["marked"])
    with execution_gate.operator_approved_command("py calc.py", cwd=cwd):
        with named_background_effect_scope("dotdot-run-script", source_context={"operating_mode": "plan", "workspace_root": cwd}):
            assert execution_gate.ExecutionGate.evaluate_command("py calc.py", cwd=cwd)["decision"] != "sandbox"


def test_off_windows_py_is_still_not_a_sandbox_program(jobs, machine) -> None:
    """Guard: the program list changes only where phase 1 pins the name (both Windows seams)."""
    from core.effect_gateway import named_background_effect_scope
    from sandbox.sandbox_runner import SandboxRunner

    runner = SandboxRunner(execution_gate.ExecutionGate(), str(machine["marked"]))
    with named_background_effect_scope("dotdot-run-script", source_context={"operating_mode": "auto", "workspace_root": str(machine["marked"])}):
        result = runner.run_command("py calc.py", cwd=str(machine["marked"]))
    assert result.get("status") == "blocked_by_policy" and "not in Sandbox whitelist" in str(result.get("error")), result
    assert jobs == []


def test_on_windows_the_sandbox_hands_py_to_phase_1(windows_sandbox, jobs, machine) -> None:
    from core.effect_gateway import named_background_effect_scope
    from sandbox.sandbox_runner import SandboxRunner

    runner = SandboxRunner(execution_gate.ExecutionGate(), str(machine["marked"]))
    with named_background_effect_scope("dotdot-run-script", source_context={"operating_mode": "auto", "workspace_root": str(machine["marked"])}):
        for command in ("py calc.py", "PY.EXE calc.py", "py.test -q"):
            result = runner.run_command(command, cwd=str(machine["marked"]))
            assert result.get("status") == "executed", (command, result)
        for command in ("node -e 1", "git status"):
            assert runner.run_command(command, cwd=str(machine["marked"])).get("status") != "executed", command
    assert [job["launched"][1:] for job in jobs] == [["calc.py"], ["calc.py"], ["-m", "pytest", "-q"]]
