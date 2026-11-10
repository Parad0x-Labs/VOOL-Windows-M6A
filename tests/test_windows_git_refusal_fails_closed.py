"""Windows 0.7.1: a refused git is "unknown", never an answer.

On Windows VOOL starts no git (core/safe_git.py). Two checks read that refusal as an answer:

- LocalLifecycle.delete_untracked_file asked git whether the file is tracked, read the refused run as
  "untracked", and deleted a tracked file (Windows CI: test_untracked_delete_refuses_tracked_file).
- user_rules.sandbox_git_check could not read the repository, so it skipped what a commit adds and what
  a push sends, and a banned word left in a commit or a push (Windows CI: 7 rows in test_user_rules).

Both now fail closed: when git is refused, or fails in any other way, the state is unknown, and the
delete or the git command is refused with the reason. Rows force the Windows decision with
core.safe_git._REFUSE_GIT (the module's test seam) and prove no git process starts while deciding.
The last rows guard Linux: what git really answers still decides there.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import safe_git
from core import user_rules as ur
from core.platform.broker import ExecutionBroker, PlatformRevocations
from core.repoops.identity import RepositoryWorkspace
from core.repoops.local_lifecycle import LocalLifecycle

REFUSAL = safe_git.GIT_REFUSED_TEXT
GLOBAL_GIT_RULE = "add this to global rules: never push the words xxx or ffff to git"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60, env=GIT_ENV)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _repo(tmp_path: Path) -> Path:
    """A real repository with one commit, made before any git start is watched."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _side_branch_with(repo: Path, branch: str, text: str) -> None:
    _git(repo, "switch", "-q", "-c", branch)
    (repo / f"{branch}.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"{branch} work")
    _git(repo, "switch", "-q", "main")


def _watch_git(monkeypatch) -> list:
    """From here on, every attempt to start git through subprocess; an attempt fails the row.
    Armed after a row has built its repository, so it watches only the decision."""
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
def windows(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)


@pytest.fixture
def linux(monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)


@pytest.fixture
def home(tmp_path):
    """A private runtime home, so the rules saved here bind only this row."""
    from core import runtime_paths
    from storage import db as storage_db

    old_env = os.environ.get("VOOL_HOME")
    old_override = runtime_paths._VOOL_HOME_OVERRIDE
    old_db = storage_db._DEFAULT_DB_PATH_OVERRIDE
    runtime = tmp_path / "home"
    (runtime / "data").mkdir(parents=True)
    os.environ["VOOL_HOME"] = str(runtime)
    runtime_paths.configure_runtime_home(runtime)
    storage_db.configure_default_db_path(runtime / "data" / "vool_web0_v2.db")
    try:
        yield runtime
    finally:
        storage_db._DEFAULT_DB_PATH_OVERRIDE = old_db
        runtime_paths.configure_runtime_home(old_override)
        if old_env is None:
            os.environ.pop("VOOL_HOME", None)
        else:
            os.environ["VOOL_HOME"] = old_env


def _say(text: str, chat: str) -> None:
    assert ur.handle_rule_command(text, chat_id=chat) is not None, text


def _delete(root: Path, rel: str):
    fork = PlatformRevocations().mint("ninja", ["fs.delete_untracked"])
    return LocalLifecycle(RepositoryWorkspace(root=str(root))).delete_untracked_file(fork, ExecutionBroker(), rel)


# ------------------------------------------------------------------------- the untracked-only delete


def test_on_windows_an_untracked_only_delete_keeps_a_tracked_file(tmp_path, windows, monkeypatch) -> None:
    root = _repo(tmp_path)
    starts = _watch_git(monkeypatch)
    outcome = _delete(root, "a.txt")  # a.txt is tracked
    assert outcome.status == "refused"
    assert "not_an_untracked_file" in outcome.reason and REFUSAL in outcome.reason
    assert (root / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert starts == []


def test_on_windows_an_untracked_only_delete_keeps_a_file_it_cannot_tell_is_untracked(tmp_path, windows,
                                                                                      monkeypatch) -> None:
    # Even a file that really is untracked is kept: without git, VOOL cannot know that it is.
    root = _repo(tmp_path)
    (root / "b.txt").write_text("new\n", encoding="utf-8")
    starts = _watch_git(monkeypatch)
    outcome = _delete(root, "b.txt")
    assert outcome.status == "refused" and REFUSAL in outcome.reason
    assert (root / "b.txt").exists()
    assert starts == []


def test_a_git_that_cannot_answer_keeps_the_file(tmp_path, linux) -> None:
    # Any git failure (here: the folder is no repository, so git cannot say) is unknown, not untracked.
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "keep.txt").write_text("keep\n", encoding="utf-8")
    outcome = _delete(folder, "keep.txt")
    assert outcome.status == "refused" and "not_an_untracked_file" in outcome.reason
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_on_linux_git_still_decides_the_untracked_only_delete(tmp_path, linux) -> None:
    root = _repo(tmp_path)
    (root / "b.txt").write_text("new\n", encoding="utf-8")
    assert _delete(root, "b.txt").status == "applied"
    assert not (root / "b.txt").exists()
    tracked = _delete(root, "a.txt")
    assert tracked.status == "refused" and "not_an_untracked_file" in tracked.reason
    assert (root / "a.txt").exists()


# ------------------------------------------------------------------------- the user-rules git check


def _assert_git_refusal(verdict: ur.GitRuleVerdict) -> None:
    assert verdict.refusal is not None, "a check that could not read the repository let the command through"
    assert verdict.refusal.incomplete and REFUSAL in verdict.refusal.reason
    assert verdict.refusal.rule_nos


@pytest.mark.parametrize("argv", [
    ["git", "commit", "-m", "update readme"],                # the staged change is unread
    ["git", "commit", "-am", "update readme"],
    ["git", "commit", "--allow-empty", "-C", "HEAD"],        # a reused message is unread
    ["git", "commit", "--amend", "--no-edit"],
    ["git", "push", "origin", "side"],                        # what a ref sends is unread
    ["git", "push", "origin", "side:refs/heads/release"],
    ["git", "push", "origin", "+side:main"],
    ["git", "push", "origin", ":"],
    ["git", "push", "--repo", "origin", "side"],
    ["git", "push", "--all", "origin"],
    ["git", "push", "--tags", "origin"],
    ["git", "push", "origin"],                                # configured refspecs and push.default are unread
    ["git", "push"],
    ["git", "push", "origin", "no-such-branch"],
])
def test_on_windows_a_commit_or_push_under_a_ban_is_refused_as_unchecked(home, tmp_path, windows, monkeypatch,
                                                                         argv) -> None:
    repo = _repo(tmp_path)
    _side_branch_with(repo, "side", "the ffff plan\n")
    _say(GLOBAL_GIT_RULE, "chat-a")
    starts = _watch_git(monkeypatch)
    _assert_git_refusal(ur.sandbox_git_check(argv, cwd=str(repo)))
    assert starts == []


def test_on_windows_a_commit_with_no_repository_known_is_refused(home, windows, monkeypatch) -> None:
    _say(GLOBAL_GIT_RULE, "chat-a")
    starts = _watch_git(monkeypatch)
    _assert_git_refusal(ur.sandbox_git_check(["git", "commit", "-m", "clean words"]))
    assert starts == []


def test_on_windows_a_word_plainly_in_the_message_is_still_named(home, tmp_path, windows, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _say(GLOBAL_GIT_RULE, "chat-a")
    starts = _watch_git(monkeypatch)
    verdict = ur.sandbox_git_check(["git", "commit", "-m", "ship ffff"], cwd=str(repo))
    assert verdict.refusal is not None and "ffff" in verdict.refusal.reason
    assert starts == []


def test_on_windows_the_command_gate_blocks_a_commit_it_cannot_check(home, tmp_path, windows, monkeypatch) -> None:
    from core.execution_gate import ExecutionGate
    from core.remote_fetch_policy import remote_fetch_policy_scope

    root = _repo(tmp_path)
    (root / "a.txt").write_text("hello ffff\n", encoding="utf-8")
    _say(GLOBAL_GIT_RULE, "chat-a")
    starts = _watch_git(monkeypatch)
    with remote_fetch_policy_scope({"surface": "openclaw", "session_id": "chat-a"}):
        decided = ExecutionGate.evaluate_command('git commit -am "update readme"', cwd=str(root))
    assert decided["decision"] == "blocked", decided
    assert decided.get("user_rule") == "refused" and REFUSAL in decided["reason"]
    assert starts == []


def test_on_windows_with_no_ban_the_rules_check_has_nothing_to_refuse(home, tmp_path, windows, monkeypatch) -> None:
    # The rules check narrows only: with no ban in force it refuses nothing (git itself is refused elsewhere).
    repo = _repo(tmp_path)
    starts = _watch_git(monkeypatch)
    assert ur.sandbox_git_check(["git", "push", "origin", "main"], cwd=str(repo)).refusal is None
    assert ur.sandbox_git_check(["git", "commit", "-m", "x"], cwd=str(repo)).refusal is None
    assert starts == []


def test_on_linux_the_rules_check_still_reads_the_repository(home, tmp_path, linux) -> None:
    repo = _repo(tmp_path)
    _side_branch_with(repo, "side", "the ffff plan\n")
    _side_branch_with(repo, "clean", "nothing to see\n")
    _say(GLOBAL_GIT_RULE, "chat-a")
    sent = ur.sandbox_git_check(["git", "push", "origin", "side"], cwd=str(repo))
    assert sent.refusal is not None and not sent.refusal.incomplete and "ffff" in sent.refusal.reason
    assert ur.sandbox_git_check(["git", "push", "origin", "clean"], cwd=str(repo)).refusal is None
    assert ur.sandbox_git_check(["git", "commit", "-m", "clean words"], cwd=str(repo)).refusal is None
