"""A chat's patch changes only the files VOOL checked, inside the chat's workspace.

The Windows write gate (core.windows_write_gate) lets `workspace.apply_unified_diff` through when the
chat's workspace is a flagged disposable folder. Before this change the patch tool then ran `git apply`
whenever the workspace sat in a git repository, and git places a git-format patch's paths from the top
of the work tree (which the repository's own `core.worktree` can move), not from the workspace. A rename
from the repository's `Documents` folder into the flagged `Inbox` therefore moved a file from outside the
flagged folder into it, and VOOL answered `no_change` because it compared only the workspace-joined
paths. A patch naming a path outside the workspace, or inside VOOL's own state, raised in the middle of
the dispatch, so the turn ended with an outcome VOOL could not tell, and a retry was blocked.

What this file pins:

- A chat whose workspace is a repository's top is refused before any approval is asked when only a
  subfolder of it is flagged.
- Every path a patch's `---`/`+++` lines name is placed before any engine runs. One outside the
  workspace or inside VOOL's own state is refused as `invalid_arguments` with nothing written, with the
  gate on and off, and the effect ledger holds a failure that is safe to retry.
- On Windows a chat's patch goes through VOOL's own engine only (reported as `protected_python`). A
  rename and a deletion are refused before any byte moves; a file with CRLF line endings is refused
  (`workspace.replace_in_file` still edits it); a patch that creates a file must name one that does not
  exist yet; a multi-file patch is all or nothing, and a file already written is put back when a later
  one changed under the patch; two sections for one file are applied in turn; a file whose section
  changes nothing is not rewritten (same file identity, same timestamp); a `git format-patch` mail
  applies; `changed_paths` and the mutation record name exactly the files written, and rolling the
  change back restores only those.
- With the gate off (off Windows), `git apply` runs only when the workspace is the top of its work tree.

Every case drives the real executor (`execute_tool_intent`) in Manual and in Auto, with real git
repositories made by the git binary and real files. A call that stops for approval (every write in
Manual, a rollback in Auto too) is approved through the real approval authority (`resolve_approval`)
and then resumed with its token, as the chat resumes a turn. The gate is switched through
`windows_write_gate._IS_WINDOWS`, which also selects the Windows patch engine. The one fault injected is
another program's write: a wrapper that edits a file and then calls the real pinned writer.

This file belongs on the Windows gauntlet's runtime list (tools/windows_fresh_host_gauntlet.ps1), where
the gate is on for real. On Linux the pinned writer takes its directory-descriptor branch; real Windows
takes its path-based branch, which only a Windows run exercises. Not pinned here: a patch creating a file
in a folder that does not exist yet (the Linux simulation and real Windows differ there).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.execution import workspace_tools
from core.execution.artifacts import latest_workspace_mutation
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from core.mode_permission_policy import reset_mode_permission_state, resolve_approval
from core.runtime_continuity import compute_logical_effect_id, get_unresolved_effect
from core.runtime_state_protection import REFUSAL_PREFIX

SECRET = b"alpha\nbeta\ngamma\n"
NOTES = b"one\ntwo\nthree\n"
NOTES_EDITED = b"one\nTWO\nthree\n"
PATCH = "workspace.apply_unified_diff"

# `git format-patch` ends a patch with its signature. VOOL strips a patch's final newline, and git refuses
# a patch that then ends inside a hunk, so this ending is what let `git apply` take the rename below (and
# move the outside file) before the fix. VOOL's own engine drops the signature, so the rename header is
# what it refuses.
SIGNATURE = "-- \n2.43.0\n"


def _rename_into(folder: str) -> str:
    """A git-format patch, paths from the repository's top, that moves Documents/secret.txt into `folder`
    and edits one line of it on the way."""
    return (
        f"diff --git a/Documents/secret.txt b/{folder}/secret.txt\n"
        "similarity index 80%\n"
        "rename from Documents/secret.txt\n"
        f"rename to {folder}/secret.txt\n"
        "--- a/Documents/secret.txt\n"
        f"+++ b/{folder}/secret.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " gamma\n"
    ) + SIGNATURE


NOTES_PATCH = (
    "diff --git a/notes.txt b/notes.txt\n"
    "index 4cb29ea..a0e5e3a 100644\n"
    "--- a/notes.txt\n"
    "+++ b/notes.txt\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    " three\n"
)


def _git(cwd: Path, *args: str) -> str:
    # An inherited GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE (git exports them to hooks) would send these
    # calls into another repository, so none of git's own variables is passed on (as core.safe_git does).
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "fx",
        "GIT_AUTHOR_EMAIL": "fx@local",
        "GIT_COMMITTER_NAME": "fx",
        "GIT_COMMITTER_EMAIL": "fx@local",
    })
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60, env=environment)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _init_repo(top: Path) -> None:
    assert shutil.which("git"), "this case needs the git binary: what it pins is how `git apply` places a patch"
    top.mkdir(parents=True, exist_ok=True)
    _git(top, "init", "-q")
    # Git for Windows' system config converts line endings on checkout and apply; this repository keeps
    # the bytes as written, so the bytes asserted below are the patch's own on every host.
    _git(top, "config", "core.autocrlf", "false")


def _commit_all(top: Path) -> str:
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "seed")
    return _git(top, "rev-parse", "HEAD").strip()


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under `root` (git's own folder aside) with its bytes, None for a folder."""
    return {
        entry.relative_to(root).as_posix(): (entry.read_bytes() if entry.is_file() else None)
        for entry in sorted(root.rglob("*"))
        if ".git" not in entry.relative_to(root).parts
    }


def _snapshot(place: Path) -> dict[str, bytes | None] | bytes:
    return _tree(place) if place.is_dir() else place.read_bytes()


def _identity(path: Path) -> tuple[int, int, int]:
    """The file's identity and timestamp: a rewrite, even with the same bytes, changes one of them."""
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_mtime_ns)


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
    calls: list[dict] = []
    real = executor.decide_tool_call

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, "decide_tool_call", recording)
    return calls


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)


@pytest.fixture
def off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)


def _flag(monkeypatch: pytest.MonkeyPatch, *folders: Path) -> None:
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join(str(folder) for folder in folders))


class _Chat:
    """One chat turn on one workspace: its session, its turn id and its operating mode."""

    def __init__(self, workspace: Path, mode: str) -> None:
        self.workspace = workspace
        self.mode = mode
        self.session_id = f"patch-chat-{uuid.uuid4().hex}"
        self.turn_id = f"patch-turn-{uuid.uuid4().hex}"

    def context(self, **extra: str) -> dict:
        return {"workspace": str(self.workspace), "session_id": self.session_id, "operating_mode": self.mode, **extra}

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
        """The call as the model makes it, once."""
        return self._execute(intent, arguments, self.context())

    def run(self, intent: str, arguments: dict, *, untouched: tuple[Path, ...] = ()):
        """The call as the operator sees it through: in Manual, asked, approved and resumed with the
        approval's token; in Auto, run at once. `untouched` are the folders and files that must not change
        while the call only waits for its approval."""
        before = {place: _snapshot(place) for place in untouched}
        first = self.ask(intent, arguments)
        if self.mode != "manual":
            return first
        assert first.status == "pending_approval", (first.status, first.response_text)
        assert first.details.get("executed") is False
        token = str(first.details["approval_request"]["approval_id"])
        approved = resolve_approval(token, decision="allow")
        assert approved is not None and approved["status"] == "approved", approved
        assert {place: _snapshot(place) for place in untouched} == before, "nothing may move before the approval"
        return self._execute(intent, arguments, self.context(mode_approval_token=token))

    def see_through(self, intent: str, arguments: dict):
        """The call made again later in the turn, or a call any mode asks about (a rollback): approved and
        resumed when it stops for an approval, in either mode."""
        first = self.ask(intent, arguments)
        if first.status != "pending_approval":
            return first
        token = str(first.details["approval_request"]["approval_id"])
        approved = resolve_approval(token, decision="allow")
        assert approved is not None and approved["status"] == "approved", approved
        return self._execute(intent, arguments, self.context(mode_approval_token=token))

    def mutation(self) -> dict | None:
        return latest_workspace_mutation(session_id=self.session_id, workspace_root=self.workspace)


def _ledger_state(arguments: dict) -> str | None:
    row = get_unresolved_effect(compute_logical_effect_id(intent=PATCH, arguments=arguments))
    return None if row is None else str(row.get("state"))


def _assert_refused_without_a_record(execution, chat: _Chat, intent: str, arguments: dict) -> None:
    assert execution.ok is False, (execution.status, execution.response_text)
    assert execution.status != "no_change", "an answer of no_change says the patch ran and found nothing to do"
    assert execution.details.get("changed_paths") in (None, []), execution.details.get("changed_paths")
    assert "mutation_record" not in execution.details
    assert chat.mutation() is None, "a refused patch records no mutation to roll back"
    # Dispatched and refused: the effect ledger holds it as a failure that is safe to retry, never as
    # an outcome still unknown.
    row = get_unresolved_effect(compute_logical_effect_id(intent=intent, arguments=arguments))
    assert row is not None and row["state"] == "failed_safe_to_retry", row and row.get("state")


def _assert_recorded(execution, chat: _Chat, changes: list[tuple[str, str, bytes, bytes]]) -> dict:
    """The executed patch's record names exactly `changes`: (path, action, bytes before, bytes after)."""
    assert execution.ok is True and execution.status == "executed", (execution.status, execution.response_text)
    assert execution.details["engine"] == "protected_python"
    assert execution.details["changed_paths"] == [path for path, _action, _before, _after in changes]
    record = chat.mutation()
    assert record is not None
    assert record["mutation_id"] == execution.details["mutation_record"]["mutation_id"]
    assert [(change["path"], change["action"], change["before_text"], change["after_text"]) for change in record["changes"]] == [
        (path, action, before.decode(), after.decode()) for path, action, before, after in changes
    ]
    return record


# ---------------------------------------------------------------------------
# A patch never moves a file from outside the flagged workspace (Windows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_rename_from_the_repository_top_into_a_flagged_subfolder_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    top = tmp_path.resolve() / "repo"
    _init_repo(top)
    (top / "Documents").mkdir()
    (top / "Documents" / "secret.txt").write_bytes(SECRET)
    inbox = top / "Inbox"
    inbox.mkdir()
    (inbox / "keep.txt").write_bytes(b"keep\n")
    head = _commit_all(top)
    _flag(monkeypatch, inbox)  # only the subfolder is flagged; the repository's top is not
    chat = _Chat(inbox, mode)
    arguments = {"patch": _rename_into("Inbox")}
    before = _tree(top)

    execution = chat.run(PATCH, arguments, untouched=(top,))

    # Documents/secret.txt keeps its place and its bytes, and nothing new appears in the flagged Inbox.
    assert _tree(top) == before
    assert _git(top, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(top, "rev-parse", "HEAD").strip() == head
    assert execution.status == "unsupported_patch", (execution.status, execution.response_text)
    assert execution.details.get("engine") == "protected_python"
    # Refused for the rename it carries (the format-patch signature is read, not refused).
    assert "`similarity index 80%`" in execution.response_text, execution.response_text
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_repository_whose_work_tree_is_moved_outside_the_flagged_folder_cannot_reach_it_through_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    """The flagged workspace holds its own `.git`, whose config sets `core.worktree` to the folder above
    it: git then reads the patch's paths from that folder, outside the flagged one."""
    home = tmp_path.resolve() / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "secret.txt").write_bytes(SECRET)
    scratch = home / "Scratch"
    _init_repo(scratch)
    _git(scratch, "config", "core.worktree", home.as_posix())
    assert Path(_git(scratch, "rev-parse", "--show-toplevel").strip()).resolve() == home
    _flag(monkeypatch, scratch)
    chat = _Chat(scratch, mode)
    arguments = {"patch": _rename_into("Scratch")}
    before = _tree(home)

    execution = chat.run(PATCH, arguments, untouched=(home,))

    # Documents/secret.txt keeps its place and its bytes, and nothing new appears in the flagged Scratch.
    assert _tree(home) == before
    assert _tree(scratch) == {}
    assert execution.status == "unsupported_patch", (execution.status, execution.response_text)
    assert execution.details.get("engine") == "protected_python"
    assert "`similarity index 80%`" in execution.response_text, execution.response_text
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_flagging_a_subfolder_does_not_let_a_chat_on_the_whole_repository_patch_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, decisions: list[dict], mode: str
) -> None:
    """The chat's workspace is the repository's top, which is not flagged; only its Inbox is. A patch
    naming Inbox/keep.txt is refused before anyone is asked to approve it and before A6 reserves it."""
    top = tmp_path.resolve() / "repo"
    _init_repo(top)
    (top / "Inbox").mkdir(parents=True)
    (top / "Inbox" / "keep.txt").write_bytes(b"keep\n")
    head = _commit_all(top)
    _flag(monkeypatch, top / "Inbox")
    reservations: list[dict] = []
    reserve = executor.reserve_logical_effect
    monkeypatch.setattr(executor, "reserve_logical_effect", lambda *a, **k: reservations.append(k) or reserve(*a, **k))
    chat = _Chat(top, mode)
    arguments = {"patch": "--- a/Inbox/keep.txt\n+++ b/Inbox/keep.txt\n@@ -1 +1 @@\n-keep\n+KEEP\n"}

    execution = chat.ask(PATCH, arguments)

    assert _tree(top) == {"Inbox": None, "Inbox/keep.txt": b"keep\n"}
    assert _git(top, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(top, "rev-parse", "HEAD").strip() == head
    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    # Refused because the repository's top is outside the one marked folder, not because none is marked.
    assert "outside the marked folders" in execution.response_text, execution.response_text
    assert execution.ok is False and execution.details.get("executed") is False
    assert decisions == [], "no approval is asked for a patch Windows refuses"
    assert reservations == []
    assert get_unresolved_effect(compute_logical_effect_id(intent=PATCH, arguments=arguments)) is None
    assert chat.mutation() is None


# ---------------------------------------------------------------------------
# A patch naming a file it may not change writes nothing, on Windows and off it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["windows", "off_windows"])
@pytest.mark.parametrize("second", ["outside_the_workspace", "vool_state"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_whose_second_file_may_not_be_changed_writes_nothing_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, mode: str, second: str,
    platform: str,
) -> None:
    """The first file is an ordinary edit inside the workspace; the second is `../outside.txt`, or a file
    in VOOL's own data folder, which sits inside the chat's folder when that folder holds VOOL's home (a
    user profile folder holding `.vool_runtime`)."""
    request.getfixturevalue(platform)
    workspace = tmp_path.resolve() / "profile"
    workspace.mkdir()
    first = workspace / "a.txt"
    first.write_bytes(NOTES)
    if second == "outside_the_workspace":
        guarded = tmp_path.resolve() / "outside.txt"
        named = "../outside.txt"
        reason = "escapes the active workspace"
    else:
        monkeypatch.setenv("VOOL_HOME", str(workspace / ".vool_runtime"))
        guarded = workspace / ".vool_runtime" / "data" / "state.txt"
        guarded.parent.mkdir(parents=True)
        named = ".vool_runtime/data/state.txt"
        reason = REFUSAL_PREFIX
    guarded.write_bytes(b"keep out\n")
    if platform == "windows":
        _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    arguments = {"patch": (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
        f"--- a/{named}\n+++ b/{named}\n@@ -1 +1 @@\n-keep out\n+KEPT OUT\n"
    )}
    first_identity = _identity(first)
    guarded_identity = _identity(guarded)

    execution = chat.run(PATCH, arguments, untouched=(first, guarded))

    assert execution.status == "invalid_arguments", (execution.status, execution.response_text)
    assert f"`{named}`" in execution.response_text and "Nothing was written" in execution.response_text, execution.response_text
    assert reason in execution.response_text, execution.response_text
    assert first.read_bytes() == NOTES and _identity(first) == first_identity, "the first file must not be touched"
    assert guarded.read_bytes() == b"keep out\n" and _identity(guarded) == guarded_identity
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)

    # The refusal is a known outcome, so the same call may be made again: it is refused the same way, not
    # blocked as a retry of an outcome VOOL cannot tell.
    again = chat.see_through(PATCH, arguments)

    assert again.status == "invalid_arguments", (again.status, again.response_text)
    assert first.read_bytes() == NOTES and guarded.read_bytes() == b"keep out\n"
    _assert_refused_without_a_record(again, chat, PATCH, arguments)


# ---------------------------------------------------------------------------
# What a patch inside the flagged workspace does (Windows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", ["no_repository", "repository_top"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_one_file_edit_inside_the_flagged_workspace_lands_exactly_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str, layout: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "notes.txt").write_bytes(NOTES)
    (workspace / "other.txt").write_bytes(b"other\n")
    head = ""
    if layout == "repository_top":
        _init_repo(workspace)
        head = _commit_all(workspace)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)

    execution = chat.run(PATCH, {"patch": NOTES_PATCH}, untouched=(workspace,))

    assert _tree(workspace) == {"notes.txt": NOTES_EDITED, "other.txt": b"other\n"}
    assert [artifact["path"] for artifact in execution.details["artifacts"]] == ["notes.txt"]
    _assert_recorded(execution, chat, [("notes.txt", "updated", NOTES, NOTES_EDITED)])
    if layout == "repository_top":
        assert _git(workspace, "status", "--porcelain", "--untracked-files=all") == " M notes.txt\n"
        assert _git(workspace, "rev-parse", "HEAD").strip() == head


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_git_format_patch_mail_applies_to_the_file_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    """The patch is `git format-patch --stdout` output, exactly as git writes it: mail headers, a
    diffstat and the signature that ends it."""
    source = tmp_path.resolve() / "source"
    _init_repo(source)
    (source / "notes.txt").write_bytes(NOTES)
    _commit_all(source)
    (source / "notes.txt").write_bytes(NOTES_EDITED)
    _git(source, "commit", "-q", "-a", "-m", "Capitalise two")
    mail = _git(source, "format-patch", "-1", "--stdout")
    assert mail.startswith("From ") and "\n-- \n" in mail, mail
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "notes.txt").write_bytes(NOTES)
    (workspace / "other.txt").write_bytes(b"other\n")
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)

    execution = chat.run(PATCH, {"patch": mail}, untouched=(workspace,))

    assert _tree(workspace) == {"notes.txt": NOTES_EDITED, "other.txt": b"other\n"}
    _assert_recorded(execution, chat, [("notes.txt", "updated", NOTES, NOTES_EDITED)])


CREATIONS = {
    # A plain unified diff creating a file in a folder that already exists.
    "plain_in_an_existing_folder": ("have/new.txt",
                                    "--- /dev/null\n+++ b/have/new.txt\n@@ -0,0 +1,2 @@\n+fresh\n+lines\n"),
    # git's own shape for a new file, at the workspace's top.
    "git_format_at_the_top": ("new.txt",
                              "diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..d1b0b7d\n"
                              "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+fresh\n+lines\n"),
}


@pytest.mark.parametrize("shape", list(CREATIONS))
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_creating_a_new_file_writes_exactly_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str, shape: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    (workspace / "have").mkdir(parents=True)
    (workspace / "notes.txt").write_bytes(NOTES)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    path, patch = CREATIONS[shape]

    execution = chat.run(PATCH, {"patch": patch}, untouched=(workspace,))

    assert _tree(workspace) == {"have": None, "notes.txt": NOTES, path: b"fresh\nlines\n"}
    record = _assert_recorded(execution, chat, [(path, "created", b"", b"fresh\nlines\n")])
    assert record["changes"][0]["existed_before"] is False


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_creating_a_file_that_already_exists_is_refused_with_its_bytes_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    notes = workspace / "notes.txt"
    notes.write_bytes(NOTES)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    arguments = {"patch": "--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1 @@\n+fresh\n"}
    identity = _identity(notes)

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert execution.status == "stale_base", (execution.status, execution.response_text)
    assert "already exists" in execution.response_text, execution.response_text
    assert notes.read_bytes() == NOTES and _identity(notes) == identity
    assert _tree(workspace) == {"notes.txt": NOTES}
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


TWO_SECTIONS_BEFORE = b"one\ntwo\nthree\nfour\nfive\nsix\nseven\n"
# The first section adds a line, so the second section's line numbers are those of the file the first
# section left: it matches only when the sections are applied in turn.
FIRST_SECTION = "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,4 @@\n one\n+one and a half\n two\n three\n"


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_two_sections_for_one_file_are_applied_in_turn_and_the_file_is_written_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "notes.txt").write_bytes(TWO_SECTIONS_BEFORE)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    patch = FIRST_SECTION + "--- a/notes.txt\n+++ b/notes.txt\n@@ -6,3 +6,3 @@\n five\n-six\n+SIX\n seven\n"
    after = b"one\none and a half\ntwo\nthree\nfour\nfive\nSIX\nseven\n"

    execution = chat.run(PATCH, {"patch": patch}, untouched=(workspace,))

    assert _tree(workspace) == {"notes.txt": after}
    # One change, from the bytes before the patch to its final bytes: never two writes, each from the original.
    _assert_recorded(execution, chat, [("notes.txt", "updated", TWO_SECTIONS_BEFORE, after)])


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_two_sections_for_one_file_whose_second_does_not_match_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    notes = workspace / "notes.txt"
    notes.write_bytes(TWO_SECTIONS_BEFORE)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    # The file says "seven" after "six", not "eight".
    arguments = {"patch": FIRST_SECTION + "--- a/notes.txt\n+++ b/notes.txt\n@@ -6,3 +6,3 @@\n five\n-six\n+SIX\n eight\n"}
    identity = _identity(notes)

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert execution.status == "stale_base", (execution.status, execution.response_text)
    assert "context mismatch" in execution.details["errors"][0], execution.details.get("errors")
    assert notes.read_bytes() == TWO_SECTIONS_BEFORE and _identity(notes) == identity, "the first section must not land alone"
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_that_only_repeats_the_files_context_leaves_the_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    notes = workspace / "notes.txt"
    notes.write_bytes(NOTES)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    arguments = {"patch": "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,3 @@\n one\n two\n three\n"}
    identity = _identity(notes)

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert execution.ok is False and execution.status == "no_change", (execution.status, execution.response_text)
    assert "changed nothing" in execution.response_text
    assert execution.details["changed_paths"] == []
    assert "mutation_record" not in execution.details
    assert chat.mutation() is None
    # Not rewritten: the same file, with its timestamp, so an editor or a build sees no change either.
    assert notes.read_bytes() == NOTES and _identity(notes) == identity
    assert _ledger_state(arguments) == "failed_safe_to_retry"


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_file_whose_section_changes_nothing_is_neither_written_nor_recorded_nor_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    edited = workspace / "a.txt"
    edited.write_bytes(NOTES)
    kept = workspace / "b.txt"
    kept.write_bytes(b"red\ngreen\nblue\n")
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,3 +1,3 @@\n red\n green\n blue\n"
    )
    kept_identity = _identity(kept)

    execution = chat.run(PATCH, {"patch": patch}, untouched=(workspace,))

    assert edited.read_bytes() == NOTES_EDITED
    assert kept.read_bytes() == b"red\ngreen\nblue\n" and _identity(kept) == kept_identity
    _assert_recorded(execution, chat, [("a.txt", "updated", NOTES, NOTES_EDITED)])

    rollback = chat.see_through("workspace.rollback_last_change", {})

    assert rollback.ok is True and rollback.status == "executed", (rollback.status, rollback.response_text)
    assert rollback.details["restored_paths"] == ["a.txt"] and rollback.details["removed_paths"] == []
    assert edited.read_bytes() == NOTES
    assert kept.read_bytes() == b"red\ngreen\nblue\n" and _identity(kept) == kept_identity


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_two_file_patch_whose_second_file_does_not_match_changes_neither_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\ntwo\nthree\n")
    (workspace / "b.txt").write_bytes(b"red\ngreen\nblue\n")
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
        # b.txt says "green", not "yellow": this section's context no longer matches the file.
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,3 +1,3 @@\n red\n-yellow\n+YELLOW\n blue\n"
    )
    arguments = {"patch": patch}

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert execution.status == "stale_base", (execution.status, execution.response_text)
    # Refused for b.txt's mismatch, not for anything about a.txt.
    assert "b.txt" in execution.response_text, execution.response_text
    assert "removal mismatch" in execution.details["errors"][0], execution.details.get("errors")
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)
    assert (workspace / "a.txt").read_bytes() == b"one\ntwo\nthree\n", "the first file must not be left half-patched"
    assert _tree(workspace) == {"a.txt": b"one\ntwo\nthree\n", "b.txt": b"red\ngreen\nblue\n"}


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_file_already_written_is_put_back_when_a_later_file_changes_under_the_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    """Another program edits b.txt after VOOL checked it and wrote a.txt, just before b.txt's write: the
    write of b.txt is refused and a.txt gets its bytes back."""
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(NOTES)
    (workspace / "b.txt").write_bytes(b"red\ngreen\nblue\n")
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    arguments = {"patch": (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,3 +1,3 @@\n red\n-green\n+GREEN\n blue\n"
    )}
    real_write = workspace_tools.pinned_atomic_write_text
    other_program: list[bytes] = []
    a_when_b_is_written: list[bytes] = []

    def another_program_edits_b_first(path, content, **kwargs):
        if Path(path) == workspace / "b.txt" and not other_program:
            a_when_b_is_written.append((workspace / "a.txt").read_bytes())
            other_program.append(b"red\nedited elsewhere\nblue\n")
            (workspace / "b.txt").write_bytes(other_program[0])
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(workspace_tools, "pinned_atomic_write_text", another_program_edits_b_first)

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert other_program, "the other program's write must have happened during the patch"
    assert a_when_b_is_written == [NOTES_EDITED], "a.txt was written before b.txt, so it had to be put back"
    assert execution.status == "stale_base", (execution.status, execution.response_text)
    assert "`b.txt`" in execution.response_text, execution.response_text
    assert execution.details["restored_paths"] == ["a.txt"] and execution.details["restore_complete"] is True
    assert _tree(workspace) == {"a.txt": NOTES, "b.txt": other_program[0]}
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_to_a_crlf_file_is_refused_with_its_bytes_kept_and_replace_in_file_still_edits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    crlf = b"one\r\ntwo\r\nthree\r\n"
    (workspace / "notes.txt").write_bytes(crlf)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    arguments = {"patch": "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"}

    refused = chat.run(PATCH, arguments, untouched=(workspace,))

    assert refused.status == "unsupported_patch", (refused.status, refused.response_text)
    assert "carriage returns" in refused.response_text, refused.response_text
    assert "workspace.replace_in_file" in refused.response_text, "the refusal names the tool that can make the change"
    _assert_refused_without_a_record(refused, chat, PATCH, arguments)
    assert (workspace / "notes.txt").read_bytes() == crlf, "the patch tool must not rewrite the line endings"

    replaced = chat.run("workspace.replace_in_file", {"path": "notes.txt", "old_text": "two", "new_text": "TWO"},
                        untouched=(workspace,))

    assert replaced.ok is True, (replaced.status, replaced.response_text)
    assert (workspace / "notes.txt").read_bytes() == b"one\r\nTWO\r\nthree\r\n"


DELETIONS = {
    "plain": ("does not delete files", "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"),
    "git_format": ("`deleted file mode 100644`",
                   "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\nindex b023018..0000000\n"
                   "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"),
}


@pytest.mark.parametrize("shape", list(DELETIONS))
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_patch_deleting_a_file_is_refused_with_the_file_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: None, mode: str, shape: str
) -> None:
    workspace = tmp_path.resolve() / "scratch"
    workspace.mkdir()
    (workspace / "gone.txt").write_bytes(b"bye\n")
    (workspace / "notes.txt").write_bytes(NOTES)
    _flag(monkeypatch, workspace)
    chat = _Chat(workspace, mode)
    reason, patch = DELETIONS[shape]
    arguments = {"patch": patch}

    execution = chat.run(PATCH, arguments, untouched=(workspace,))

    assert _tree(workspace) == {"gone.txt": b"bye\n", "notes.txt": NOTES}
    assert execution.status == "unsupported_patch", (execution.status, execution.response_text)
    assert execution.details.get("engine") == "protected_python"
    assert reason in execution.response_text, execution.response_text
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


# ---------------------------------------------------------------------------
# Off Windows: `git apply` runs only from the top of the work tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", ["subfolder", "core_worktree"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_off_windows_a_rename_from_the_repository_top_into_a_workspace_that_is_not_the_top_moves_nothing(
    tmp_path: Path, off_windows: None, mode: str, layout: str
) -> None:
    """`subfolder`: the workspace is the repository's Inbox. `core_worktree`: the workspace holds its own
    `.git`, whose config moves the work tree's top to the folder above it."""
    top = tmp_path.resolve() / "repo"
    (top / "Documents").mkdir(parents=True)
    (top / "Documents" / "secret.txt").write_bytes(SECRET)
    if layout == "subfolder":
        _init_repo(top)
        workspace = top / "Inbox"
        workspace.mkdir()
        (workspace / "keep.txt").write_bytes(b"keep\n")
        head = _commit_all(top)
    else:
        workspace = top / "Scratch"
        _init_repo(workspace)
        _git(workspace, "config", "core.worktree", top.as_posix())
        head = ""
    assert Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve() == top
    chat = _Chat(workspace, mode)
    arguments = {"patch": _rename_into(workspace.name)}
    before = _tree(top)

    execution = chat.run(PATCH, arguments, untouched=(top,))

    assert _tree(top) == before
    if head:
        assert _git(top, "status", "--porcelain", "--untracked-files=all") == ""
        assert _git(top, "rev-parse", "HEAD").strip() == head
    # git is not run from a workspace that is not the top of its work tree: VOOL's own engine reads the
    # patch's paths from the workspace, finds no such file there, and answers that the patch does not match.
    assert execution.status == "stale_base", (execution.status, execution.response_text)
    assert "context mismatch" in execution.details["errors"][0], execution.details.get("errors")
    _assert_refused_without_a_record(execution, chat, PATCH, arguments)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_off_windows_a_patch_in_a_workspace_that_is_the_repository_top_still_goes_through_git(
    tmp_path: Path, off_windows: None, mode: str
) -> None:
    workspace = tmp_path.resolve() / "repo"
    _init_repo(workspace)
    (workspace / "notes.txt").write_bytes(NOTES)
    head = _commit_all(workspace)
    chat = _Chat(workspace, mode)

    execution = chat.run(PATCH, {"patch": NOTES_PATCH + SIGNATURE}, untouched=(workspace,))

    assert execution.ok is True and execution.status == "executed", (execution.status, execution.response_text)
    assert execution.details["engine"] == "git_apply"
    assert (workspace / "notes.txt").read_bytes() == NOTES_EDITED
    assert execution.details["changed_paths"] == ["notes.txt"]
    record = chat.mutation()
    assert record is not None and [change["path"] for change in record["changes"]] == ["notes.txt"]
    assert _git(workspace, "status", "--porcelain", "--untracked-files=all") == " M notes.txt\n"
    assert _git(workspace, "rev-parse", "HEAD").strip() == head
