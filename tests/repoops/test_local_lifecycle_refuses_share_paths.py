"""LocalLifecycle never reaches for a Windows share path it is handed.

On Windows, a stat of `\\\\host\\share` is an SMB connection (and an NTLM hash sent to whoever
answers), so "refused, but only after a lookup" is still the bug. create_worktree and
remove_worktree resolved the path with os.path.realpath (and create checked os.path.exists) before
anything refused it, and stage_exact_files resolved each path the same way (reproduced 21 Oct 2026
at port 13a11d8 with the touch recorder). delete_untracked_file stats (and could unlink) the joined
path, which is the share itself when the share is absolute, and restore_worktree_file and unstage_file
hand it to git as a pathspec, which git resolves the same way. Each now asks
core.windows_path_policy first.

A local path is still added, removed and staged as before.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.platform.broker import ExecutionBroker, PlatformRevocations
from core.repoops.identity import RepositoryWorkspace
from core.repoops.local_lifecycle import LocalLifecycle, StageScopeError
from tests._nonlocal_touch_recorder import recorder, windows_host

TOKENS = ["git.worktree", "git.restore", "git.unstage", "fs.delete_untracked"]
SHARES = ["\\\\attkr9\\s\\x", "//attkr9/s/x", "\\\\?\\UNC\\attkr9\\s\\x", "\\\\.\\UNC\\attkr9\\s\\x"]
# Plain ids: a test id naming the marker host would reach every process through PYTEST_CURRENT_TEST.
SHARE_IDS = ["unc", "forward-slashes", "long-unc", "device-unc"]


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
    _git(root, "branch", "feat")
    return root


def _lifecycle(repo: Path) -> tuple[LocalLifecycle, object, ExecutionBroker]:
    return LocalLifecycle(RepositoryWorkspace(root=str(repo))), PlatformRevocations().mint("ninja", TOKENS), \
        ExecutionBroker()


@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_a_worktree_on_a_share_is_refused_before_it_is_looked_up(repo, recorder, windows_host, share) -> None:
    lc, fork, broker = _lifecycle(repo)
    windows_host.on()

    receipt = lc.create_worktree(fork, broker, share, "feat")

    assert receipt.status != "applied", receipt
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_removing_a_worktree_on_a_share_is_refused_before_it_is_looked_up(repo, recorder, windows_host,
                                                                         share) -> None:
    lc, fork, broker = _lifecycle(repo)
    windows_host.on()

    receipt = lc.remove_worktree(fork, broker, share)

    assert receipt.status != "applied", receipt
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_staging_a_share_path_is_refused_before_it_is_looked_up(repo, recorder, windows_host, share) -> None:
    lc, _fork, _broker = _lifecycle(repo)
    windows_host.on()

    with pytest.raises(StageScopeError):
        lc.stage_exact_files((share,))

    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


@pytest.mark.parametrize("step", ["restore_worktree_file", "unstage_file", "delete_untracked_file"])
@pytest.mark.parametrize("share", SHARES, ids=SHARE_IDS)
def test_restoring_unstaging_or_deleting_a_share_path_is_refused_before_it_is_looked_up(
        repo, recorder, windows_host, share, step) -> None:
    lc, fork, broker = _lifecycle(repo)
    windows_host.on()

    receipt = getattr(lc, step)(fork, broker, share)

    assert receipt.status != "applied", receipt
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def test_a_local_file_is_still_restored_unstaged_and_deleted(repo, windows_host) -> None:
    lc, fork, broker = _lifecycle(repo)
    windows_host.on()
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")
    (repo / "new.txt").write_text("scratch\n")

    unstaged = lc.unstage_file(fork, broker, "a.txt")
    restored = lc.restore_worktree_file(fork, broker, "a.txt")
    deleted = lc.delete_untracked_file(fork, broker, "new.txt")

    assert [r.status for r in (unstaged, restored, deleted)] == ["applied"] * 3, (unstaged, restored, deleted)
    assert (repo / "a.txt").read_text() == "one\n"
    assert not (repo / "new.txt").exists()


def test_a_local_worktree_is_still_added_and_removed_and_a_local_file_still_staged(repo, tmp_path,
                                                                                    windows_host) -> None:
    target = tmp_path / "feat-wt"
    lc, fork, broker = _lifecycle(repo)
    windows_host.on()

    added = lc.create_worktree(fork, broker, str(target), "feat")
    removed = lc.remove_worktree(fork, broker, str(target))
    (repo / "a.txt").write_text("two\n")
    staged = lc.stage_exact_files(("a.txt",))

    assert added.status == "applied" and removed.status == "applied", (added, removed)
    assert not target.exists()
    assert staged == ("a.txt",)
