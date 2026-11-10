"""On Windows, a git operation runs only when its work tree's top and its git folders are inside marked folders.

repo.git runs git in the repo session's folder, and git writes under the top of that repository's
work tree, in its git folder and in its shared git folder. The repository's own layout can put each
of those outside the session's folder: a session opened on a subfolder of a bigger repository,
`core.worktree` pointing above the folder, a `.git` file pointing to a git folder elsewhere, a
linked worktree whose main repository is elsewhere, or a linked worktree whose own git folder was
moved elsewhere. The Windows write gate used to check a repo.git call only on the chat's workspace,
so from a chat in a marked folder (VOOL_WINDOWS_DISPOSABLE_WORKSPACES) a commit in a repository
outside the marked folders reached the approval ask and, once approved, ran; sent straight to the
production tool door it ran with no ask at all. Now the gate asks git itself where those places
are, and unless all of them are inside the marked folders the call is refused before anyone is
asked to approve it (and again at dispatch), the refusal names the outside place, and nothing in
the repository changes. The session's folder is read the way the repo runtime reads it: while its
journal is gone or cannot be read or parsed, from the runtime's cached copy. The gate fails closed:
a repository whose own config names a program for git to run, as core/safe_git.py reads it, is not
asked at all, and one whose config cannot be read or that git cannot place is refused too. A
repository wholly inside a marked folder still reaches the approval ask in Manual and Auto, and the
approved commit runs. Folders below those three places (a link in place of `.git/objects`) and a
repository-local `lfs.storage` setting are not placed, so some of git's writes (objects, refs, logs)
can still land elsewhere through them; this file does not cover them.

Sessions are opened, inspected and bound through the production tool door, every repository is real
(made with the git binary; the global git config is out of reach because HOME, USERPROFILE and
XDG_CONFIG_HOME point at an empty folder), and repo.git goes through the real executor with a spy
that calls through to the real permission decision. repo.git takes no A6 reservation, so there is
no A6 row to check. The Windows decision is switched on through the gate's `_IS_WINDOWS`, so the
file runs on every platform and is on the Windows gauntlet's runtime list.

Re-pinned for 0.7.1 (VOOL doesn't run git on Windows, core/safe_git.py): every repo.git call in this
file is now refused before the permission decision with the git sentence, marked folder or not, and
VOOL starts no git process while the call is decided (`_on_windows` records any git start and fails
the row). The sessions are opened with the gate off (`_as_before_0_7_1`: a session opened before
0.7.1, or on another platform), since on Windows repo.session.open is itself refused. Row names are
kept so each row maps to the earlier contract; they name the layout each row builds, which no longer
decides the outcome.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from core import safe_git
from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig

MODES = ("manual", "auto")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "fx",
    "GIT_AUTHOR_EMAIL": "fx@local",
    "GIT_COMMITTER_NAME": "fx",
    "GIT_COMMITTER_EMAIL": "fx@local",
}
_OUTSIDE = re.compile(r"This call also changes [^(]+ \((.+)\), which is outside the marked folders; nothing was changed\.")
REFUSAL = safe_git.GIT_REFUSED_TEXT
#: Every git start attempted while a call is decided on Windows (cleared by the `world` fixture).
_GIT_STARTS: list = []


@contextlib.contextmanager
def _as_before_0_7_1():
    """The gate and core/safe_git.py's git refusal off, as when the session was opened before 0.7.1 or
    on another platform. On a real Windows host safe_git refuses git from the platform itself, so the
    gate's seam alone would leave repo.session.open refused there."""
    saved_gate, saved_git = gate._IS_WINDOWS, safe_git._REFUSE_GIT
    gate._IS_WINDOWS, safe_git._REFUSE_GIT = False, False
    try:
        yield
    finally:
        gate._IS_WINDOWS, safe_git._REFUSE_GIT = saved_gate, saved_git


@contextlib.contextmanager
def _on_windows():
    """Windows as 0.7.1 decides it (the gate and core/safe_git.py), every git start recorded and failed."""
    saved_gate, saved_git, real = gate._IS_WINDOWS, safe_git._REFUSE_GIT, subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            if safe_git.is_git_command(args, kwargs.get("executable")):
                _GIT_STARTS.append(args)
                raise AssertionError(f"git was started on Windows: {args!r}")
            super().__init__(args, *rest, **kwargs)

    gate._IS_WINDOWS, safe_git._REFUSE_GIT, subprocess.Popen = True, True, Recording
    try:
        yield
    finally:
        gate._IS_WINDOWS, safe_git._REFUSE_GIT, subprocess.Popen = saved_gate, saved_git, real


def _git(cwd: Path, *args: str) -> str:
    """The real git, without the system config or inherited GIT_* settings; the user's global config
    is out of reach because the `world` fixture points the home folders at an empty one."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(_IDENTITY, GIT_CONFIG_NOSYSTEM="1")
    out = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                         capture_output=True, encoding="utf-8", timeout=60, env=env)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _seed(cwd: Path) -> None:
    """The commit identity in the repository's own config, where VOOL's own git finds it, then one
    commit of everything in the work tree."""
    _git(cwd, "config", "user.name", "fx")
    _git(cwd, "config", "user.email", "fx@local")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", "seed")


def _repo(top: Path) -> Path:
    """A repository whose work tree top and git folder are both `top`."""
    top.mkdir(parents=True, exist_ok=True)
    (top / "README.md").write_bytes(b"seed\n")
    _git(top, "init", "-q", "-b", "main")
    _seed(top)
    return top


def _state(folder: Path) -> dict[str, object]:
    """Everything a commit run in `folder` would change: HEAD, every ref, the index's bytes and the
    working tree's status."""
    index = Path(_git(folder, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    return {
        "head": _git(folder, "rev-parse", "HEAD").strip(),
        "branch": _git(folder, "symbolic-ref", "-q", "HEAD").strip(),
        "refs": _git(folder, "for-each-ref", "--format=%(refname) %(objectname)"),
        "index": index.read_bytes() if index.exists() else None,
        "status": _git(folder, "status", "--porcelain", "--untracked-files=all"),
    }


def _files(folder: Path) -> dict[str, bytes]:
    """Every file under `folder` with its bytes, git's own files included: the state of a repository
    git itself can no longer read."""
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}


def _door(intent: str, arguments: dict, folder: Path):
    from core.runtime_execution_tools import execute_runtime_tool

    context = {"workspace": str(folder), "workspace_root": str(folder), "session_id": f"repo-open-{uuid.uuid4().hex}",
               "operating_mode": "auto"}
    with _as_before_0_7_1():
        result = execute_runtime_tool(intent, arguments, source_context=context)
    assert result is not None and result.ok, (intent, result and result.status, result and result.response_text)
    return result


def _bound_session(folder: Path) -> str:
    """A repo session on `folder`, opened, inspected and bound through the production door from a
    chat whose workspace is that folder."""
    opened = _door("repo.session.open", {"objective": "commit the pending change"}, folder)
    session = opened.details["repo_session_id"]
    _door("repo.inspect", {"repo_session_id": session}, folder)
    _door("repo.bind", {"repo_session_id": session}, folder)
    return session


def _journal_steps(session: str) -> dict:
    """The steps the session's journal on disk records (repo.git records one before it runs git)."""
    from core.repoops import plane

    return dict(json.loads(plane.session_journal_path(session).read_text(encoding="utf-8"))["steps"])


def _spy(monkeypatch, name: str) -> list[dict]:
    calls: list[dict] = []
    real = getattr(executor, name)

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, name, recording)
    return calls


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    from core.mode_permission_policy import reset_mode_permission_state
    from core.repoops.plane import repo_ops_runtime

    # VOOL's own git keeps HOME (safe_git strips only GIT_* settings), so the user's global config
    # (commit signing, a hooks path) would otherwise decide whether these commits run.
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    reset_mode_permission_state()
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox"))
    monkeypatch.setenv("VOOL_REPOOPS_DIR", str(tmp_path / "repo_sessions"))
    repo_ops_runtime().reset()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _GIT_STARTS.clear()
    try:
        yield {"decide_tool_call": _spy(monkeypatch, "decide_tool_call"), "git_starts": _GIT_STARTS}
    finally:
        repo_ops_runtime().reset()
        reset_mode_permission_state()


def _flag(monkeypatch, *folders: Path) -> None:
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join(str(folder) for folder in folders))


def _commit(session: str, chat: Path, mode: str, *, task_id: str = "", chat_session: str = "", token: str = ""):
    context = {"workspace": str(chat), "operating_mode": mode}
    if token:
        context["mode_approval_token"] = token
    tracker = HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None))
    with _on_windows():
        return executor.execute_tool_intent(
            {"intent": "repo.git",
             "arguments": {"repo_session_id": session, "operation": "commit", "message": "commit the pending change"}},
            task_id=task_id or f"repo-git-{uuid.uuid4().hex}",
            session_id=chat_session or f"repo-git-session-{uuid.uuid4().hex}",
            checkpoint_id=f"runtime-{uuid.uuid4().hex}",
            step_index=0,
            source_context=context,
            hive_activity_tracker=tracker,
        )


def _assert_names_one_of(text: str, outside: tuple[Path, ...]) -> None:
    """The refusal's own sentence names, in parentheses, one of the `outside` places (compared as
    files, so separators, case and short names do not matter; the path may hold parentheses)."""
    found = _OUTSIDE.search(text)
    assert found is not None, text
    named = Path(found.group(1))
    assert named.exists() and any(os.path.samefile(named, where) for where in outside), (
        [str(where) for where in outside], text)


def _assert_refused_before_approval(execution, spies: dict) -> None:
    """Refused by the Windows gate before the permission decision with the git sentence, nothing
    executed and no git process started."""
    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    assert execution.ok is False and execution.details.get("executed") is False
    assert spies["decide_tool_call"] == [], "no approval is asked for a git operation Windows refuses"
    assert execution.response_text == REFUSAL, execution.response_text
    assert "nothing was changed" in execution.response_text, execution.response_text
    assert spies["git_starts"] == [], "VOOL starts no git process on Windows"


def _assert_refused_naming(execution, spies: dict, *, outside: tuple[Path, ...]) -> None:
    """Refused before the permission decision with the git sentence (0.7.1). Before 0.7.1 the refusal
    named one of the `outside` places git would write in; git is not asked where it writes now."""
    _assert_refused_before_approval(execution, spies)
    assert not _OUTSIDE.search(execution.response_text), execution.response_text


@pytest.mark.parametrize("mode", MODES)
def test_a_commit_in_a_repository_outside_the_marked_folders_is_refused_before_approval(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    marked = tmp_path / "marked"
    marked.mkdir()
    _flag(monkeypatch, marked)
    repo = _repo(tmp_path / "real-project")
    session = _bound_session(repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)

    # The chat is in the marked folder; the session it names runs git in a repository outside it.
    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(repo, repo / ".git"))
    assert _state(repo) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_commit_in_a_repository_inside_a_marked_folder_reaches_approval_and_the_approved_commit_runs(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """0.7.1: a marked folder does not let git run on Windows; the commit is refused before approval."""
    marked = tmp_path / "marked"
    repo = _repo(marked / "project")
    _flag(monkeypatch, marked)
    session = _bound_session(repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)
    task, chat = f"repo-git-{uuid.uuid4().hex}", f"repo-git-session-{uuid.uuid4().hex}"

    asked = _commit(session, marked, mode, task_id=task, chat_session=chat)

    _assert_refused_before_approval(asked, world)
    assert "approval_request" not in asked.details, "no approval card for git that never runs"
    assert _state(repo) == before, "nothing ran"
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("journal", ("gone", "a-folder", "not-json", "a-json-list"))
def test_a_commit_whose_session_journal_cannot_be_read_is_checked_on_the_cached_session_folder(
    tmp_path: Path, monkeypatch, world, journal: str, mode: str
) -> None:
    """While the session's journal is gone, cannot be read or does not parse as a session, the repo
    runtime keeps serving its cached copy, and would run git in that copy's folder: the gate reads the
    same copy."""
    from core.repoops import plane

    marked = tmp_path / "marked"
    marked.mkdir()
    _flag(monkeypatch, marked)
    repo = _repo(tmp_path / "real-project")
    session = _bound_session(repo)
    path = plane.session_journal_path(session)
    if journal in ("gone", "a-folder"):
        path.unlink()
        if journal == "a-folder":
            path.mkdir()
    else:
        path.write_bytes(b"{not json" if journal == "not-json" else b"[]")

    def on_disk() -> object:
        return "dir" if path.is_dir() else (path.read_bytes() if path.exists() else None)

    journal_before = on_disk()
    # The runtime, in this process, still serves the session on the repository outside the marked folder.
    served = plane.repo_ops_runtime()._load(session)
    assert served is not None and os.path.samefile(served.root, repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)

    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(repo, repo / ".git"))
    assert _state(repo) == before
    assert on_disk() == journal_before


@pytest.mark.parametrize("mode", MODES)
def test_a_session_on_a_marked_subfolder_of_an_unmarked_repository_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    top = tmp_path / "monorepo"
    service = top / "service"
    service.mkdir(parents=True)
    (service / "app.py").write_bytes(b"print('app')\n")
    _repo(top)
    _flag(monkeypatch, service)
    session = _bound_session(service)
    (service / "change.txt").write_bytes(b"pending\n")
    (top / "elsewhere.txt").write_bytes(b"outside the marked folder\n")
    before = _state(service)

    execution = _commit(session, service, mode)

    _assert_refused_naming(execution, world, outside=(top, top / ".git"))
    assert _state(service) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_repository_whose_core_worktree_points_above_it_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    outer = tmp_path / "outer"
    project = outer / "project"
    project.mkdir(parents=True)
    (outer / "outside.txt").write_bytes(b"outside\n")
    (project / "inside.txt").write_bytes(b"inside\n")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "core.worktree", outer.as_posix())
    _seed(project)
    _flag(monkeypatch, project)
    session = _bound_session(project)
    (project / "change.txt").write_bytes(b"pending\n")
    (outer / "outside.txt").write_bytes(b"changed outside\n")
    before = _state(project)
    # The change outside the marked folder is one the commit would take in.
    assert " M outside.txt" in str(before["status"]), before["status"]

    execution = _commit(session, project, mode)

    _assert_refused_naming(execution, world, outside=(outer,))
    assert _state(project) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_folder_whose_git_file_points_to_a_git_folder_outside_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "README.md").write_bytes(b"seed\n")
    store = tmp_path / "elsewhere.git"
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={store}", str(marked))
    assert (marked / ".git").is_file() and (marked / ".git").read_bytes().startswith(b"gitdir: ")
    _seed(marked)
    _flag(monkeypatch, marked)
    session = _bound_session(marked)
    (marked / "change.txt").write_bytes(b"pending\n")
    before = _state(marked)

    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(store,))
    assert _state(marked) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_linked_worktree_of_an_unmarked_repository_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    main = _repo(tmp_path / "main-repo")
    marked = tmp_path / "marked"
    marked.mkdir()
    linked = marked / "linked"
    _git(main, "worktree", "add", "-q", "-b", "linked-work", str(linked))
    _flag(monkeypatch, marked)
    session = _bound_session(linked)
    (linked / "change.txt").write_bytes(b"pending\n")
    before, before_main = _state(linked), _state(main)

    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(main / ".git" / "worktrees" / "linked", main / ".git"))
    assert _state(linked) == before and _state(main) == before_main
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_linked_worktree_whose_shared_git_folder_alone_is_outside_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """The work tree and the worktree's own git folder are marked; only the main repository's git
    folder, where the commit's objects and branch go, is not."""
    main = _repo(tmp_path / "main-repo")
    marked = tmp_path / "marked"
    marked.mkdir()
    linked = marked / "linked"
    _git(main, "worktree", "add", "-q", "-b", "linked-work", str(linked))
    private = Path(_git(linked, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    _flag(monkeypatch, marked, private)
    session = _bound_session(linked)
    (linked / "change.txt").write_bytes(b"pending\n")
    before, before_main = _state(linked), _state(main)

    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(main / ".git",))
    assert _state(linked) == before and _state(main) == before_main
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_linked_worktree_whose_own_git_folder_alone_is_outside_is_refused(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """The work tree and the main repository are marked; the worktree's own git folder, where its
    HEAD and index are written, was moved outside (its `.git` file and `commondir` file updated, the
    layout gitrepository-layout(5) describes)."""
    main = _repo(tmp_path / "main-repo")
    marked = tmp_path / "marked"
    marked.mkdir()
    linked = marked / "linked"
    _git(main, "worktree", "add", "-q", "-b", "linked-work", str(linked))
    private = tmp_path / "elsewhere" / "linked"
    private.parent.mkdir()
    (main / ".git" / "worktrees" / "linked").rename(private)
    # Removed before it is written again: Git for Windows marks a `.git` file it creates hidden, and
    # Windows refuses to overwrite a hidden file in place.
    (linked / ".git").unlink()
    (linked / ".git").write_bytes(f"gitdir: {private.as_posix()}\n".encode())
    (private / "commondir").write_bytes(f"{(main / '.git').as_posix()}\n".encode())
    places = _git(linked, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir")
    expected = (linked, private, main / ".git")
    assert len(places.splitlines()) == 3, places
    assert all(os.path.samefile(line, where) for line, where in zip(places.splitlines(), expected, strict=True)), places
    _flag(monkeypatch, marked, main)
    session = _bound_session(linked)
    (linked / "change.txt").write_bytes(b"pending\n")
    before, before_main = _state(linked), _state(main)

    execution = _commit(session, marked, mode)

    _assert_refused_naming(execution, world, outside=(private,))
    assert _state(linked) == before and _state(main) == before_main
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_marked_repository_whose_config_names_a_program_is_refused_before_approval(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """The gate asks git where a repository writes only when the repository's own config names no
    program for git to run; otherwise it refuses in the repository runner's own words."""
    from core.repoops.gitops import GitRunner

    marked = tmp_path / "marked"
    repo = _repo(marked / "project")
    _flag(monkeypatch, marked)
    session = _bound_session(repo)
    _git(repo, "config", "core.sshCommand", "vool-test-program-that-must-not-run")
    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)

    execution = _commit(session, marked, mode)

    _assert_refused_before_approval(execution, world)
    # Before 0.7.1 the refusal gave the runner's words naming core.sshCommand; git is not asked now.
    with _as_before_0_7_1():
        assert "core.sshcommand" in GitRunner(root=repo)._repository_refusal(mutating=False).lower()
    assert _state(repo) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
def test_a_repository_git_cannot_place_is_refused_before_approval(
    tmp_path: Path, monkeypatch, world, mode: str
) -> None:
    """A marked repository whose config now says it has no work tree: its config names no program,
    but git cannot say where the work tree is, so the gate cannot check where git writes."""
    marked = tmp_path / "marked"
    repo = _repo(marked / "project")
    _flag(monkeypatch, marked)
    session = _bound_session(repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    _git(repo, "config", "core.bare", "true")
    before = _files(marked)

    execution = _commit(session, marked, mode)

    _assert_refused_before_approval(execution, world)
    assert _files(marked) == before
    assert _journal_steps(session) == {}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("layout", ("malformed-config", "git-file-to-a-missing-folder"))
def test_a_repository_whose_git_config_cannot_be_read_is_refused_before_approval(
    tmp_path: Path, monkeypatch, world, layout: str, mode: str
) -> None:
    """A marked repository whose own config git cannot read (a malformed line, or a `.git` file that
    points at a git folder that is gone): the gate cannot check it, so the call is refused."""
    marked = tmp_path / "marked"
    repo = _repo(marked / "project")
    _flag(monkeypatch, marked)
    session = _bound_session(repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    if layout == "malformed-config":
        config = repo / ".git" / "config"
        config.write_bytes(config.read_bytes() + b"[core\n")
    else:
        (repo / ".git").rename(marked / "parked.git")
        (repo / ".git").write_bytes(f"gitdir: {(marked / 'missing.git').as_posix()}\n".encode())
    before = _files(marked)

    execution = _commit(session, marked, mode)

    _assert_refused_before_approval(execution, world)
    assert _files(marked) == before
    assert _journal_steps(session) == {}


def test_a_commit_sent_straight_to_the_production_door_is_refused_at_dispatch(
    tmp_path: Path, monkeypatch, world
) -> None:
    """A call that reaches dispatch without the executor's early check (the door a skill or plugin
    proposal crosses, which takes no permission decision of its own) meets the same check there."""
    from core.runtime_execution_tools import execute_runtime_tool

    marked = tmp_path / "marked"
    marked.mkdir()
    _flag(monkeypatch, marked)
    repo = _repo(tmp_path / "real-project")
    session = _bound_session(repo)
    (repo / "change.txt").write_bytes(b"pending\n")
    before = _state(repo)
    context = {"workspace": str(marked), "workspace_root": str(marked), "session_id": f"repo-git-{uuid.uuid4().hex}",
               "operating_mode": "auto"}

    with _on_windows():
        result = execute_runtime_tool(
            "repo.git", {"repo_session_id": session, "operation": "commit", "message": "commit the pending change"},
            source_context=context,
        )

    assert result is not None
    assert result.status == gate.STATUS, (result.status, result.response_text)
    assert result.ok is False and result.details.get("executed") is False
    assert result.response_text == REFUSAL, result.response_text
    assert world["git_starts"] == []
    assert _state(repo) == before
    assert _journal_steps(session) == {}
