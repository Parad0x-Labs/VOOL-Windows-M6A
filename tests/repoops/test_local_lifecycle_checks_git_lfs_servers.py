"""LocalLifecycle's own checkouts never send Git LFS to a server the repository chose.

core/repoops/local_lifecycle.py checked a repository's config and hooks without the command, so
GitRunner's Git LFS check (core/safe_git.py lfs_endpoint_refusal) never ran there: `worktree add`
checked out a branch whose committed .lfsconfig named lfs.url=https://evil.example, which GitRunner's
own merge of that branch refuses (reproduced 20 Oct 2026). With the user's git-lfs filter installed,
that checkout's smudge step asks that server for every LFS file. Each mutation now passes its argv.

A repository without such an .lfsconfig, or one naming its own git host, still works as before.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.platform.broker import ExecutionBroker, PlatformRevocations
from core.repoops import local_lifecycle
from core.repoops.identity import RepositoryWorkspace
from core.repoops.local_lifecycle import LocalLifecycle, capture_snapshot

TOKENS = ["git.switch_branch", "git.delete_branch", "git.restore", "git.worktree", "git.cherry_pick"]
HOSTILE = "[lfs]\n\turl = https://evil.example/lfs\n"


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
    _git(root, "remote", "add", "origin", "https://github.com/example/project.git")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _branch_with(repo: Path, branch: str, lfsconfig: str) -> str:
    """Commit ``lfsconfig`` as .lfsconfig on a new ``branch`` and return to main; the commit's id."""
    _git(repo, "switch", "-q", "-c", branch)
    (repo / ".lfsconfig").write_text(lfsconfig)
    _git(repo, "add", ".lfsconfig")
    _git(repo, "commit", "-qm", "lfs settings")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    return sha


def _lifecycle(repo: Path) -> tuple[LocalLifecycle, object, ExecutionBroker]:
    return LocalLifecycle(RepositoryWorkspace(root=str(repo))), PlatformRevocations().mint("ninja", TOKENS), \
        ExecutionBroker()


def test_adding_a_worktree_of_a_branch_naming_another_lfs_server_is_refused(repo, tmp_path) -> None:
    _branch_with(repo, "feat", HOSTILE)
    target = tmp_path / "feat-wt"
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.create_worktree(fork, broker, str(target), "feat")
    # The receipt's reason is cut short after the long path, so the refusal is read where it is made.
    direct = local_lifecycle._run(str(repo), "worktree", "add", str(target), "feat", mutating=True)

    assert receipt.status != "applied", receipt
    assert "git was" in (receipt.reason or ""), receipt
    assert direct.returncode != 0 and "evil.example" in direct.stderr, direct.stderr
    assert not target.exists()


def test_restoring_a_file_while_the_index_names_another_lfs_server_is_refused(repo) -> None:
    (repo / ".lfsconfig").write_text(HOSTILE)
    _git(repo, "add", ".lfsconfig")
    (repo / ".lfsconfig").unlink()  # only the index carries it
    (repo / "a.txt").write_text("changed\n")
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.restore_worktree_file(fork, broker, "a.txt")

    assert receipt.status != "applied", receipt
    assert "evil.example" in (receipt.reason or ""), receipt
    assert (repo / "a.txt").read_text() == "changed\n"


def test_switching_to_a_branch_naming_another_lfs_server_is_refused(repo) -> None:
    _branch_with(repo, "feat", HOSTILE)
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.switch_branch(fork, broker, "feat")

    assert receipt.status != "applied", receipt
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert not (repo / ".lfsconfig").exists()


def test_cherry_picking_a_commit_naming_another_lfs_server_is_refused(repo) -> None:
    sha = _branch_with(repo, "feat", HOSTILE)
    head = _git(repo, "rev-parse", "HEAD")
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.cherry_pick(fork, broker, sha, expected_head=head, expected_branch="main")

    assert receipt.status != "applied", receipt
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".lfsconfig").exists()


# -- the ordinary operations still run ----------------------------------------------------------

def test_a_worktree_of_an_ordinary_branch_is_still_added(repo, tmp_path) -> None:
    _git(repo, "branch", "review")
    target = tmp_path / "review-wt"
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.create_worktree(fork, broker, str(target), "review")

    assert receipt.status == "applied", receipt
    assert (target / "a.txt").read_text() == "one\n"


def test_a_branch_naming_its_own_git_host_for_lfs_is_still_checked_out(repo, tmp_path) -> None:
    _branch_with(repo, "feat", "[lfs]\n\turl = https://github.com/example/project.git/info/lfs\n")
    lc, fork, broker = _lifecycle(repo)

    added = lc.create_worktree(fork, broker, str(tmp_path / "feat-wt"), "feat")
    _git(repo, "worktree", "remove", str(tmp_path / "feat-wt"))
    switched = lc.switch_branch(fork, broker, "feat")

    assert added.status == "applied", added
    assert switched.status == "applied", switched
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "feat"


def test_a_file_is_still_restored_and_a_clean_commit_still_cherry_picked(repo) -> None:
    _git(repo, "switch", "-q", "-c", "side")
    (repo / "b.txt").write_text("two\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "b")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("changed\n")
    lc, fork, broker = _lifecycle(repo)

    restored = lc.restore_worktree_file(fork, broker, "a.txt")
    head = capture_snapshot(RepositoryWorkspace(root=str(repo))).head_sha
    picked = lc.cherry_pick(fork, broker, sha, expected_head=head, expected_branch="main")

    assert restored.status == "applied", restored
    assert (repo / "a.txt").read_text() == "one\n"
    assert picked.status == "applied", picked
    assert (repo / "b.txt").read_text() == "two\n"


def test_a_branch_carrying_another_lfs_server_can_still_be_deleted(repo) -> None:
    _branch_with(repo, "feat", HOSTILE)
    lc, fork, broker = _lifecycle(repo)

    receipt = lc.delete_local_branch(fork, broker, "feat", force_unmerged=True)

    assert receipt.status == "applied", receipt
    assert "feat" not in _git(repo, "branch", "--list", "feat")
