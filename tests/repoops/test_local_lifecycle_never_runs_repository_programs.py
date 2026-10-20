"""LocalLifecycle runs git without letting the repository run programs.

core/repoops/local_lifecycle.py ran a plain `git -C <root> ...` for every operation: the commit ran the
repository's pre-commit, commit-msg and post-commit hooks, a core.hooksPath hook, a core.fsmonitor and
a local gpg.program (reproduced 20 Oct 2026). Every git call there now goes through core/safe_git.py:
a repository whose own config names a program is refused, a mutation is refused when hooks would
run, and the rest runs hardened with a scrubbed environment.

Each planted program writes a marker; the marker and the repository's HEAD are read back from disk.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from core.platform.broker import ExecutionBroker, PlatformRevocations
from core.repoops.identity import RepositoryWorkspace, WorkspaceError
from core.repoops.local_lifecycle import LocalLifecycle, capture_snapshot

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the planted programs are POSIX shell scripts")

TOKENS = ["git.switch_branch", "git.commit", "git.worktree"]


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "n@x")
    _git(root, "config", "user.name", "Ninja")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _program(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho ran > '{marker}'\nexit 0\n")
    path.chmod(0o755)


def _staged_commit(root: Path):
    """Stage f.txt on a clean repository and return a commit call to make after planting."""
    ws = RepositoryWorkspace(root=str(root))
    lc = LocalLifecycle(ws)
    (root / "f.txt").write_text("data\n")
    lc.stage_exact_files(("f.txt",))
    head = capture_snapshot(ws).head_sha
    staged = hashlib.sha256(_git(root, "diff", "--cached").encode()).hexdigest()[:16]

    def commit():
        return lc.commit_with_proposal(PlatformRevocations().mint("ninja", TOKENS), ExecutionBroker(), "add f",
                                       expected_head=head, expected_staged_hash=staged)

    return commit, head


@pytest.mark.parametrize("hook", ["pre-commit", "commit-msg", "post-commit"])
def test_a_hook_planted_before_the_commit_never_runs(repo, tmp_path, hook) -> None:
    commit, head = _staged_commit(repo)
    marker = tmp_path / "HOOK_RAN"
    _program(repo / ".git" / "hooks" / hook, marker)

    receipt = commit()

    assert receipt.status != "applied", receipt
    assert "git was not run" in (receipt.reason or "")
    assert not marker.exists()
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("key", ["core.fsmonitor", "core.hooksPath", "gpg.program"])
def test_a_program_named_by_the_repository_config_never_runs(repo, tmp_path, key) -> None:
    commit, head = _staged_commit(repo)
    marker = tmp_path / "PROGRAM_RAN"
    program = tmp_path / "program.sh"
    _program(program, marker)
    if key == "core.hooksPath":
        _program(tmp_path / "hooks" / "pre-commit", marker)
        _git(repo, "config", key, str(tmp_path / "hooks"))
    else:
        _git(repo, "config", key, str(program))
        if key == "gpg.program":
            _git(repo, "config", "commit.gpgsign", "true")

    with pytest.raises(WorkspaceError, match="git was not run"):
        commit()  # its first git call, the snapshot's status, refuses

    assert not marker.exists()
    assert _git(repo, "rev-parse", "HEAD") == head


def test_switching_branch_never_runs_a_post_checkout_hook(repo, tmp_path) -> None:
    _git(repo, "branch", "review")
    marker = tmp_path / "POST_CHECKOUT_RAN"
    _program(repo / ".git" / "hooks" / "post-checkout", marker)

    receipt = LocalLifecycle(RepositoryWorkspace(root=str(repo))).switch_branch(
        PlatformRevocations().mint("ninja", TOKENS), ExecutionBroker(), "review")

    assert receipt.status != "applied", receipt
    assert not marker.exists()
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_adding_a_worktree_never_runs_a_post_checkout_hook(repo, tmp_path) -> None:
    _git(repo, "branch", "review")
    marker = tmp_path / "POST_CHECKOUT_RAN"
    _program(repo / ".git" / "hooks" / "post-checkout", marker)
    target = tmp_path / "review-wt"

    receipt = LocalLifecycle(RepositoryWorkspace(root=str(repo))).create_worktree(
        PlatformRevocations().mint("ninja", TOKENS), ExecutionBroker(), str(target), "review")

    assert receipt.status != "applied", receipt
    assert not marker.exists()
    assert not target.exists()


def test_an_inherited_git_config_environment_cannot_inject_a_program(repo, tmp_path, monkeypatch) -> None:
    marker = tmp_path / "INJECTED_RAN"
    program = tmp_path / "program.sh"
    _program(program, marker)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(program))
    (repo / "a.txt").write_text("changed\n")

    snapshot = capture_snapshot(RepositoryWorkspace(root=str(repo)))

    assert snapshot.unstaged_paths == ("a.txt",)
    assert not marker.exists()


def test_a_clean_repository_still_commits(repo) -> None:
    commit, head = _staged_commit(repo)

    receipt = commit()

    assert receipt.status == "applied", receipt
    assert receipt.evidence["parent_verified"] is True
    assert _git(repo, "rev-parse", "HEAD") == receipt.evidence["sha"] != head
