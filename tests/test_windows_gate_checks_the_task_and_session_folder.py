"""A code-task step or rollback, or a repo step, is checked on the folder it really writes in.

A code task writes in the folder it was opened in (its journal's `workspace_root`), and a repo session
in the folder it was opened on; a chat can call either one later from a different workspace. The
Windows write gate (core.windows_write_gate) checked these calls only on the chat's workspace. From a
chat in a flagged disposable folder, a step or a rollback on a task or session in a folder that is not
flagged therefore reached the approval ask (in Auto a repo step was dispatched at once) and was refused
only further in, where the inner write is checked on its real folder: by then the repo session's
journal had recorded the step and a fault, and an allowed rollback had minted a rollback authorization
bound to the folder that is not flagged. A step whose inner intent carried a space
(` workspace.apply_unified_diff`), which both step runtimes strip before they run it, was not gated at all.

Such a call is now refused before the permission decision, with nothing journaled or written, and the
refusal names the folder. The gate reads that folder the way each runtime reads it: a task's from its
journal, also after VOOL restarts; a session's from its journal, after a restart too, or, while that
journal is missing or cannot be read, from the copy the repo runtime still holds and would run. A task
journal whose folder is not an absolute path is refused as one whose folder cannot be checked. A task
or session in a flagged folder reaches the approval as before; once allowed, a repo step's patch lands
through VOOL's own engine and names exactly the file it changed. A task or session the runtime cannot
load (a journal cut short, a task record without its folder or inside a JSON list, or no journal and
nothing held) is still checked on the chat's folder, and the runtime answers that no such task or
session exists, writing nothing.

The rollback cases carry no recorded change to restore, because reaching one needs a sandboxed
reproduction. They pin the refusal before the decision and that no rollback authorization is minted;
before this change, approving the rollback on a folder that is not flagged minted one bound to it.

Every case drives the real executor (`execute_tool_intent`) in Manual and in Auto, on real code tasks
and repo sessions opened through the production tool door, in real git repositories made with the git
binary. Approvals go through the real approval authority (`resolve_approval`) and the call is resumed
with its token, as the chat resumes a turn. The gate is switched on through
`windows_write_gate._IS_WINDOWS`, and the file is written to run on Windows as well.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from core.mode_permission_policy import reset_mode_permission_state, resolve_approval
from tests.repoops._forge_fixture import UNIFIED_DIFF, RecordedForge, github_pull_request

BUGGY = b"def add(a, b):\n    return a - b\n"
FIXED = b"def add(a, b):\n    return a + b\n"
NOTES = b"work in progress\n"
REPAIRS = {
    "workspace.write_file": {"path": "calc.py", "content": FIXED.decode()},
    # The forge's diff of pull request 7: the repair of calc.py.
    "workspace.apply_unified_diff": {"patch": UNIFIED_DIFF},
}
MODES = ["manual", "auto"]
RESTARTS = pytest.mark.parametrize("restarted", [False, True], ids=["same-run", "after-restart"])
TASK_FOLDER, SESSION_FOLDER = "the code task's folder", "the repo session's folder"
#: The gate's refusal of a call whose task or session folder is outside the marked folders.
_OUTSIDE = re.compile(
    r"This call also changes (?P<label>.+?) \((?P<folder>.+)\), which is outside the marked folders; "
    r"nothing was changed\."
)


def _git(cwd: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "fx",
        "GIT_AUTHOR_EMAIL": "fx@local",
        "GIT_COMMITTER_NAME": "fx",
        "GIT_COMMITTER_EMAIL": "fx@local",
    }
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60, env=environment)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _repository(top: Path) -> None:
    """A repository with calc.py's defect on main and one more commit on the branch `feature`."""
    assert shutil.which("git"), "these cases run code tasks and repo sessions in real git repositories"
    top.mkdir(parents=True)
    _git(top, "init", "-q", "-b", "main")
    # Git for Windows' system config converts line endings; this repository keeps the bytes as written.
    _git(top, "config", "core.autocrlf", "false")
    (top / "calc.py").write_bytes(BUGGY)
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "seed")
    _git(top, "switch", "-q", "-c", "feature")
    (top / "NOTES.md").write_bytes(NOTES)
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "start the repair")


def _head(top: Path) -> str:
    return _git(top, "rev-parse", "HEAD").strip()


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root` (git's own folder aside) with its bytes, None for a folder."""
    return {
        entry.relative_to(root).as_posix(): (entry.read_bytes() if entry.is_file() else None)
        for entry in sorted(root.rglob("*"))
        if ".git" not in entry.relative_to(root).parts
    }


def _flag(monkeypatch: pytest.MonkeyPatch, *folders: Path) -> None:
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join(str(folder) for folder in folders))


def _door(intent: str, arguments: dict, context: dict):
    from core.runtime_execution_tools import execute_runtime_tool

    result = execute_runtime_tool(intent, dict(arguments), source_context=dict(context))
    assert result is not None, f"{intent} is not contracted at the production door"
    return result


def _rollback_authorizations() -> list[dict]:
    from core.blackbox.store import default_store

    return [entry for entry in default_store().entries() if entry.get("kind") == "rollback_authorized"]


def _task_journal(task_id: str) -> Path:
    from core.code_assistant.task_runtime import task_dir

    return task_dir() / f"{task_id}.json"


def _session_journal(session_key: str) -> Path:
    from core.repoops.plane import session_journal_path

    return session_journal_path(session_key)


def _session_store() -> Path:
    from core.repoops.plane import session_dir

    return session_dir()


def _restart_vool() -> None:
    """VOOL restarted: the runtimes hold nothing in memory, and a task or a session lives only in its journal."""
    from core.code_assistant.task_runtime import code_task_runtime
    from core.repoops.plane import repo_ops_runtime

    repo_ops_runtime().reset()
    code_task_runtime().reset()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.code_assistant.task_runtime import code_task_runtime
    from core.repoops.plane import repo_ops_runtime

    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox"))
    monkeypatch.setenv("VOOL_REPOOPS_DIR", str(tmp_path / "repo_sessions"))
    monkeypatch.setenv("VOOL_CODE_TASK_DIR", str(tmp_path / "code_tasks"))
    reset_mode_permission_state()
    repo_ops_runtime().reset()
    code_task_runtime().reset()
    try:
        yield
    finally:
        code_task_runtime().reset()
        repo_ops_runtime().reset()
        reset_mode_permission_state()


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The gate switched on as on Windows, and every permission decision the executor takes recorded on its
    way through to the real `decide_tool_call`."""
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    calls: list[dict] = []
    real = executor.decide_tool_call

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, "decide_tool_call", recording)
    return calls


@pytest.fixture
def forge():
    """The pull request's forge, answered from recorded replies through the shipped GitHub adapter."""
    from core.repoops import forge as forge_bridge

    recorded = RecordedForge()
    forge_bridge.install_transport_factory(recorded.factory)
    try:
        yield recorded
    finally:
        forge_bridge.install_transport_factory(None)


class _Chat:
    """One chat on one workspace: its session, its turn id and its operating mode."""

    def __init__(self, workspace: Path, mode: str) -> None:
        self.workspace = workspace
        self.mode = mode
        self.session_id = f"folder-chat-{uuid.uuid4().hex}"
        self.turn_id = f"folder-turn-{uuid.uuid4().hex}"

    def context(self, **extra: str) -> dict:
        return {"workspace": str(self.workspace), "session_id": self.session_id, "operating_mode": self.mode, **extra}

    def working_in(self, folder: Path) -> dict:
        """This chat's context at the time it worked in `folder`, where it opened a task or a session."""
        return {"workspace": str(folder), "workspace_root": str(folder), "session_id": self.session_id,
                "operating_mode": self.mode}

    def _execute(self, intent: str, arguments: dict, context: dict):
        return executor.execute_tool_intent(
            {"intent": intent, "arguments": dict(arguments)},
            task_id=self.turn_id,
            session_id=self.session_id,
            checkpoint_id=f"runtime-{uuid.uuid4().hex}",
            step_index=0,
            source_context=context,
            hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
        )

    def ask(self, intent: str, arguments: dict):
        """The call as the model makes it, once, from this chat's workspace."""
        return self._execute(intent, arguments, self.context())

    def approve_and_resume(self, asked, intent: str, arguments: dict):
        """The operator allows the call the chat was asked about, and the chat resumes it with the token."""
        assert asked.status == "pending_approval", (asked.status, asked.response_text)
        assert asked.details.get("executed") is False
        token = str(asked.details["approval_request"]["approval_id"])
        approved = resolve_approval(token, decision="allow")
        assert approved is not None and approved["status"] == "approved", approved
        return self._execute(intent, arguments, self.context(mode_approval_token=token))


def _open_task(chat: _Chat, folder: Path) -> str:
    opened = _door("code.task.open", {"objective": "Find why add subtracts and repair it"}, chat.working_in(folder))
    assert opened.ok, (opened.status, opened.response_text)
    task_id = opened.details["task_id"]
    # The task writes in the folder it was opened in, whatever the chat's workspace is later.
    assert Path(json.loads(_task_journal(task_id).read_text(encoding="utf-8"))["workspace_root"]) == folder
    return task_id


def _diagnosed_session(chat: _Chat, folder: Path, forge) -> str:
    """A repo session on `folder`, bound to pull request 7 and at its repair stage: the forge's diff of
    the pull request was read, calc.py was read through a step, and that read is the diagnosis's evidence.
    A repair step needs that diagnosis, and none of this runs a sandbox."""
    context = chat.working_in(folder)
    head = _head(folder)
    forge.route("GET /pulls/7", github_pull_request("7", base_sha=_git(folder, "rev-parse", "HEAD~1").strip(), head_sha=head))
    forge.route("GET /commits/feature", {"sha": head})
    opened = _door(
        "repo.session.open",
        {"objective": "repair add in calc.py", "pull_request": "7", "provider": "github", "namespace": "o/r",
         "reviewer_model": "anthropic/claude-sonnet-4"},
        context,
    )
    assert opened.ok, (opened.status, opened.response_text)
    session = opened.details["repo_session_id"]
    for intent in ("repo.inspect", "repo.bind"):
        done = _door(intent, {"repo_session_id": session}, context)
        assert done.ok, (intent, done.status, done.response_text)
    forge.route("GET /pulls/7", UNIFIED_DIFF)
    diff = _door("repo.diff", {"repo_session_id": session}, context)
    assert diff.ok and diff.details["paths"] == ["calc.py"], (diff.status, diff.response_text)
    read = _door("repo.step", {"repo_session_id": session, "step_id": "read", "intent": "workspace.read_file",
                               "arguments": {"path": "calc.py"}}, context)
    assert read.ok and read.details["executed"] is True, (read.status, read.response_text)
    diagnosed = _door("repo.diagnose", {"repo_session_id": session, "path": "calc.py", "reason": "add subtracts",
                                        "evidence_step_id": "read"}, context)
    assert diagnosed.ok, (diagnosed.status, diagnosed.response_text)
    assert Path(json.loads(_session_journal(session).read_bytes())["root"]) == folder
    return session


def _assert_refused_before_approval(execution, decisions: list[dict], label: str, folder: Path) -> None:
    """Refused by the gate before the permission decision, so no approval is asked for it and nothing is
    dispatched; the refusal names the task's or session's folder the call would have written in."""
    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    assert decisions == [], "refused before the permission decision: no approval is asked for it"
    assert execution.ok is False and execution.details.get("executed") is False
    named = _OUTSIDE.search(execution.response_text)
    assert named is not None, execution.response_text
    assert named.group("label") == label, execution.response_text
    assert Path(named.group("folder")) == folder, execution.response_text


# ---------------------------------------------------------------------------
# Code tasks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@RESTARTS
def test_a_code_task_step_on_a_task_folder_that_is_not_flagged_is_refused_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], restarted: bool, mode: str
) -> None:
    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch)  # the chat's folder is flagged; the task's folder is not
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    if restarted:
        _restart_vool()
    journal = _task_journal(task_id)
    journal_before, project_before, head = journal.read_bytes(), _tree(project), _head(project)
    arguments = {"task_id": task_id, "step_id": "repair", "intent": "workspace.write_file",
                 "arguments": dict(REPAIRS["workspace.write_file"])}

    execution = chat.ask("code.task.step", arguments)

    _assert_refused_before_approval(execution, decisions, TASK_FOLDER, project)
    # The task journal records no step (in flight or refused) and no fault for the call.
    assert journal.read_bytes() == journal_before
    record = json.loads(journal_before)
    assert record["steps"] == {} and record["step_order"] == [] and record["fault_ids"] == []
    assert _tree(project) == project_before and _head(project) == head
    assert _git(project, "status", "--porcelain", "--untracked-files=all") == ""
    assert _tree(scratch) == {}


@pytest.mark.parametrize("mode", MODES)
@RESTARTS
def test_a_code_task_rollback_on_a_task_folder_that_is_not_flagged_is_refused_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], restarted: bool, mode: str
) -> None:
    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    if restarted:
        _restart_vool()
    journal = _task_journal(task_id)
    journal_before, project_before, head = journal.read_bytes(), _tree(project), _head(project)
    arguments = {"task_id": task_id}

    execution = chat.ask("code.task.rollback", arguments)

    _assert_refused_before_approval(execution, decisions, TASK_FOLDER, project)
    # No rollback was authorized, none is journaled, and the task's folder is as it was. (The task has
    # recorded no change: reaching one needs a sandboxed reproduction. Before this change an approval
    # of this call minted a rollback authorization bound to the folder that is not flagged.)
    assert _rollback_authorizations() == []
    assert journal.read_bytes() == journal_before
    assert json.loads(journal_before)["rollback"] == {"restored": False, "restored_paths": []}
    assert _tree(project) == project_before and _head(project) == head
    assert _git(project, "status", "--porcelain", "--untracked-files=all") == ""
    assert _tree(scratch) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_code_task_step_on_a_flagged_task_folder_still_reaches_approval_and_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], mode: str
) -> None:
    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch, project)
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    journal = _task_journal(task_id)
    journal_before, project_before = journal.read_bytes(), _tree(project)
    arguments = {"task_id": task_id, "step_id": "repair", "intent": "workspace.write_file",
                 "arguments": dict(REPAIRS["workspace.write_file"])}

    # Auto asks for a code-task step too, and the ask names the task's file.
    asked = chat.ask("code.task.step", arguments)

    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    assert len(decisions) == 1
    # Compared as paths: on Windows the ask joins the task's folder and the file with "/".
    shown = asked.details["approval_request"]["affected_resources"]
    assert [Path(resource) for resource in shown] == [project / "calc.py"], shown
    assert journal.read_bytes() == journal_before and _tree(project) == project_before

    resumed = chat.approve_and_resume(asked, "code.task.step", arguments)

    # Allowed, the call reaches the task, whose own law refuses a repair nobody proposed or approved.
    assert resumed.status == "stage_violation", (resumed.status, resumed.response_text)
    assert json.loads(journal.read_bytes())["steps"]["repair"]["status"] == "stage_violation"
    assert _tree(project) == project_before
    assert _tree(scratch) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_code_task_rollback_on_a_flagged_task_folder_still_reaches_approval_and_runs_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], mode: str
) -> None:
    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch, project)
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    project_before = _tree(project)
    arguments = {"task_id": task_id}

    asked = chat.ask("code.task.rollback", arguments)

    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    assert len(decisions) == 1
    assert _rollback_authorizations() == []

    resumed = chat.approve_and_resume(asked, "code.task.rollback", arguments)

    # The rollback ran in the task's folder: its authorization is bound to that folder, and the Blackbox
    # answers that the task's turn recorded nothing there to restore (the task has no recorded change;
    # reaching one needs a sandboxed reproduction).
    assert resumed.status == "blackbox_unknown_turn", (resumed.status, resumed.response_text)
    assert [Path(entry["root"]) for entry in _rollback_authorizations()] == [project]
    assert json.loads(_task_journal(task_id).read_bytes())["rollback"]["status"] == "blackbox_unknown_turn"
    assert _tree(project) == project_before
    assert _tree(scratch) == {}


def _cut_short(raw: bytes) -> bytes:
    return raw[: len(raw) // 2]


def _without_folder(raw: bytes) -> bytes:
    record = json.loads(raw)
    del record["workspace_root"]
    return json.dumps(record, sort_keys=True, indent=1).encode("utf-8")


def _inside_a_list(raw: bytes) -> bytes:
    return json.dumps([json.loads(raw)], sort_keys=True, indent=1).encode("utf-8")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("intent", ["code.task.step", "code.task.rollback"])
@pytest.mark.parametrize("damage", [_cut_short, _without_folder, _inside_a_list],
                         ids=["cut-short", "no-workspace-root", "json-list"])
def test_a_task_journal_the_runtime_cannot_load_is_checked_on_the_chats_folder_and_answered_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], damage, intent: str, mode: str
) -> None:
    """The gate finds no folder for a task the runtime would not load either (`CodeTaskRuntime._load` reads
    the journal through `CodeTask.from_json` and keeps no copy of one it cannot read), so it checks the
    chat's folder as it always did, and the runtime answers that no such task exists without writing. The
    journal is a real task's, opened in a folder that is not flagged, then damaged."""
    from core.code_assistant.task_runtime import code_task_runtime, task_dir

    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    (scratch / "calc.py").write_bytes(BUGGY)
    _repository(project)
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    journal = _task_journal(task_id)
    damaged = damage(journal.read_bytes())
    journal.write_bytes(damaged)
    assert code_task_runtime()._load(task_id) is None, "the task runtime finds no such task"
    tasks_before, project_before, head = _tree(task_dir()), _tree(project), _head(project)
    arguments = (
        {"task_id": task_id, "step_id": "repair", "intent": "workspace.write_file",
         "arguments": dict(REPAIRS["workspace.write_file"])}
        if intent == "code.task.step" else {"task_id": task_id}
    )

    asked = chat.ask(intent, arguments)

    assert asked.status == "pending_approval", (asked.status, asked.response_text)
    assert len(decisions) == 1, "the call reaches the permission decision, as before"
    assert _tree(task_dir()) == tasks_before and _tree(scratch) == {"calc.py": BUGGY}

    answer = chat.approve_and_resume(asked, intent, arguments)

    assert answer.status == "unknown_task", (answer.status, answer.response_text)
    assert answer.ok is False
    assert _tree(task_dir()) == tasks_before and journal.read_bytes() == damaged
    assert _tree(scratch) == {"calc.py": BUGGY}
    assert _tree(project) == project_before and _head(project) == head
    assert _rollback_authorizations() == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("intent", ["code.task.step", "code.task.rollback"])
def test_a_task_whose_folder_is_not_an_absolute_path_is_refused_as_one_that_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], intent: str, mode: str
) -> None:
    """A task journal that names its folder as a relative path still loads, but where that folder is
    depends on VOOL's working directory when the call runs, so the gate cannot place it and refuses the
    call before the decision (fail closed). Here the name, read from VOOL's working directory, would even
    be the flagged folder."""
    from core.code_assistant.task_runtime import code_task_runtime

    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    task_id = _open_task(chat, project)
    journal = _task_journal(task_id)
    record = json.loads(journal.read_bytes())
    record["workspace_root"] = scratch.name
    journal.write_bytes(json.dumps(record, sort_keys=True, indent=1).encode("utf-8"))
    monkeypatch.chdir(tmp_path)
    loaded = code_task_runtime()._load(task_id)
    assert loaded is not None and loaded.workspace_root == scratch.name, "the task runtime loads this task"
    journal_before, project_before, head = journal.read_bytes(), _tree(project), _head(project)
    arguments = (
        {"task_id": task_id, "step_id": "repair", "intent": "workspace.write_file",
         "arguments": dict(REPAIRS["workspace.write_file"])}
        if intent == "code.task.step" else {"task_id": task_id}
    )

    execution = chat.ask(intent, arguments)

    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    assert decisions == [], "refused before the permission decision: no approval is asked for it"
    assert execution.ok is False and execution.details.get("executed") is False
    assert task_id in execution.response_text, execution.response_text
    assert "cannot be checked" in execution.response_text, execution.response_text
    assert "nothing was changed" in execution.response_text, execution.response_text
    assert journal.read_bytes() == journal_before
    assert _tree(project) == project_before and _head(project) == head
    assert _tree(scratch) == {}
    assert _rollback_authorizations() == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("padded", ["workspace.write_file ", " workspace.apply_unified_diff"])
@pytest.mark.parametrize("step", ["code.task.step", "repo.step"])
def test_a_step_whose_inner_intent_is_padded_is_still_checked_on_its_own_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], forge, step: str, padded: str, mode: str
) -> None:
    """Both step runtimes strip the inner intent before they classify and run it, so to them a padded
    intent is the write it names. The gate strips it the same way; before this change a padded write was
    not gated at all and reached the permission decision: Manual asked for it, and Auto asked for it or ran it."""
    scratch, project = tmp_path.resolve() / "scratch", tmp_path.resolve() / "project"
    scratch.mkdir()
    _repository(project)
    _flag(monkeypatch, scratch)  # the chat's folder is flagged; the task's or session's folder is not
    chat = _Chat(scratch, mode)
    if step == "repo.step":
        owner = {"repo_session_id": _diagnosed_session(chat, project, forge)}
        journal, label = _session_journal(owner["repo_session_id"]), SESSION_FOLDER
    else:
        owner = {"task_id": _open_task(chat, project)}
        journal, label = _task_journal(owner["task_id"]), TASK_FOLDER
    journal_before, project_before, head = journal.read_bytes(), _tree(project), _head(project)
    arguments = {**owner, "step_id": "repair", "intent": padded, "arguments": dict(REPAIRS[padded.strip()])}

    execution = chat.ask(step, arguments)

    _assert_refused_before_approval(execution, decisions, label, project)
    # The journal records no step for the call (in flight or refused) and no fault.
    assert journal.read_bytes() == journal_before
    record = json.loads(journal_before)
    assert "repair" not in record["steps"]
    assert record["step_order"] == (["read"] if step == "repo.step" else [])
    if step == "code.task.step":
        assert record["fault_ids"] == []
    assert _tree(project) == project_before and _head(project) == head
    assert _git(project, "status", "--porcelain", "--untracked-files=all") == ""
    assert _tree(scratch) == {}


# ---------------------------------------------------------------------------
# Repo sessions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("inner", sorted(REPAIRS))
@RESTARTS
def test_a_repo_step_on_a_session_folder_that_is_not_flagged_is_refused_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], forge, restarted: bool, inner: str,
    mode: str
) -> None:
    scratch, repo = tmp_path.resolve() / "scratch", tmp_path.resolve() / "repo"
    scratch.mkdir()
    _repository(repo)
    _flag(monkeypatch, scratch)  # the chat's folder is flagged; the session's folder is not
    chat = _Chat(scratch, mode)
    session = _diagnosed_session(chat, repo, forge)
    if restarted:
        _restart_vool()
    journal = _session_journal(session)
    journal_before, repo_before, head = journal.read_bytes(), _tree(repo), _head(repo)
    arguments = {"repo_session_id": session, "step_id": "repair", "intent": inner, "arguments": dict(REPAIRS[inner])}

    execution = chat.ask("repo.step", arguments)

    _assert_refused_before_approval(execution, decisions, SESSION_FOLDER, repo)
    # The session journal records no step for the call (in flight or refused) and no fault.
    assert journal.read_bytes() == journal_before
    assert json.loads(journal_before)["step_order"] == ["read"]
    assert _tree(repo) == repo_before and _head(repo) == head
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _tree(scratch) == {}


def _damage_session_journal(journal: Path, state: str) -> None:
    if state == "missing":
        journal.unlink()
    else:
        journal.write_bytes(_cut_short(journal.read_bytes()))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("journal_state", ["cut-short", "missing"])
def test_a_repo_step_whose_journal_cannot_be_read_is_checked_on_the_session_the_runtime_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], forge, journal_state: str, mode: str
) -> None:
    """While a session's journal is missing or cannot be read, the repo runtime keeps serving the copy it
    holds (`RepoOpsRuntime._load`), and a step would run in that copy's folder: the gate checks that folder."""
    from core.repoops.plane import repo_ops_runtime

    scratch, repo = tmp_path.resolve() / "scratch", tmp_path.resolve() / "repo"
    scratch.mkdir()
    _repository(repo)
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    session = _diagnosed_session(chat, repo, forge)
    _damage_session_journal(_session_journal(session), journal_state)
    held = repo_ops_runtime()._load(session)
    assert held is not None and Path(held.root) == repo, "the repo runtime still serves the session"
    sessions_before, repo_before, head = _tree(_session_store()), _tree(repo), _head(repo)
    arguments = {"repo_session_id": session, "step_id": "repair", "intent": "workspace.write_file",
                 "arguments": dict(REPAIRS["workspace.write_file"])}

    execution = chat.ask("repo.step", arguments)

    _assert_refused_before_approval(execution, decisions, SESSION_FOLDER, repo)
    assert _tree(_session_store()) == sessions_before
    assert _tree(repo) == repo_before and _head(repo) == head
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _tree(scratch) == {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("journal_state", ["cut-short", "missing"])
def test_a_repo_session_the_runtime_cannot_find_is_checked_on_the_chats_folder_and_answered_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], forge, journal_state: str, mode: str
) -> None:
    """After a restart, with the journal missing or unreadable, the repo runtime holds no copy and finds no
    session, so the gate finds no folder either: it checks the chat's folder as it always did, and the
    runtime answers that no such session exists without writing."""
    from core.repoops.plane import repo_ops_runtime

    scratch, repo = tmp_path.resolve() / "scratch", tmp_path.resolve() / "repo"
    scratch.mkdir()
    (scratch / "calc.py").write_bytes(BUGGY)
    _repository(repo)
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    session = _diagnosed_session(chat, repo, forge)
    _damage_session_journal(_session_journal(session), journal_state)
    _restart_vool()
    assert repo_ops_runtime()._load(session) is None, "the repo runtime finds no such session"
    sessions_before, repo_before, head = _tree(_session_store()), _tree(repo), _head(repo)
    arguments = {"repo_session_id": session, "step_id": "repair", "intent": "workspace.write_file",
                 "arguments": dict(REPAIRS["workspace.write_file"])}

    asked = chat.ask("repo.step", arguments)

    assert len(decisions) == 1, "the call reaches the permission decision, as before"
    if mode == "manual":
        assert _tree(_session_store()) == sessions_before and _tree(scratch) == {"calc.py": BUGGY}
        answer = chat.approve_and_resume(asked, "repo.step", arguments)
    else:
        answer = asked
    assert answer.status == "unknown_session", (answer.status, answer.response_text)
    assert answer.ok is False
    assert _tree(_session_store()) == sessions_before
    assert _tree(scratch) == {"calc.py": BUGGY}
    assert _tree(repo) == repo_before and _head(repo) == head


@pytest.mark.parametrize("mode", MODES)
def test_a_repo_step_patch_on_a_flagged_session_folder_lands_through_vools_own_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: list[dict], forge, mode: str
) -> None:
    scratch, repo = tmp_path.resolve() / "scratch", tmp_path.resolve() / "repo"
    scratch.mkdir()
    _repository(repo)
    _flag(monkeypatch, scratch, repo)
    chat = _Chat(scratch, mode)
    session = _diagnosed_session(chat, repo, forge)
    repo_before, head = _tree(repo), _head(repo)
    arguments = {"repo_session_id": session, "step_id": "repair", "intent": "workspace.apply_unified_diff",
                 "arguments": dict(REPAIRS["workspace.apply_unified_diff"])}

    asked = chat.ask("repo.step", arguments)

    assert len(decisions) == 1, "the call reaches the permission decision"
    # Manual asks first and nothing moves before the approval; Auto runs a repair in the session's folder.
    assert asked.status == ("pending_approval" if mode == "manual" else "executed"), (asked.status, asked.response_text)
    if mode == "manual":
        assert _tree(repo) == repo_before
        execution = chat.approve_and_resume(asked, "repo.step", arguments)
    else:
        execution = asked
    assert execution.ok is True and execution.status == "executed", (execution.status, execution.response_text)
    result = execution.details["tool_result"]
    assert result["engine"] == "protected_python"
    assert result["changed_paths"] == ["calc.py"]
    assert _tree(repo) == {**repo_before, "calc.py": FIXED}
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == " M calc.py\n"
    assert _head(repo) == head
    assert json.loads(_session_journal(session).read_bytes())["steps"]["repair"]["status"] == "executed"
    assert _tree(scratch) == {}
