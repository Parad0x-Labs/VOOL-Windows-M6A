"""A program named in a repository's ``config.worktree``, or in a checked-out submodule's config,
must never run, exactly like one in its ``config`` (core/safe_git.py).

Reproduced 2026-10-19: with ``extensions.worktreeConfig`` on, a ``filter.<x>.clean`` set by
``git config --worktree`` was missed by the ``--local``-only check, and a repo.git commit
(GitRunner.commit, the path repo.git takes) ran it, on every platform. Each case plants a program
that leaves a marker file if git runs it. A control first proves git really would run the planted
program on this platform, so a missing marker means VOOL refused it, not that the plant was inert.
Found in review the same day: status, diff and commit in a superproject run git inside each
checked-out submodule, which obeys the submodule's own config.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import safe_git
from core.execution.git_tools import git_diff_workspace, git_status_workspace
from core.repoops.gitops import GitRunner

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.test")
    (repo / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    return repo, tmp_path / "MARKER"


def _planted_program(tmp_path: Path, marker: Path) -> str:
    # A working clean filter (stdin to stdout) that also leaves the marker.
    script = tmp_path / "planted.py"
    script.write_text(
        f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    python = sys.executable.replace("\\", "/")
    return f'"{python}" "{script.as_posix()}"'


def _plant_worktree_filter(repo: Path, tmp_path: Path, marker: Path) -> None:
    _git(repo, "config", "--worktree", "filter.planted.clean", _planted_program(tmp_path, marker))
    (repo / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")


def test_control_hardened_git_alone_still_runs_a_worktree_config_filter(tmp_path: Path) -> None:
    # Without the config check, hardening and the scrubbed environment do not stop it: the
    # check is what closes this.
    repo, marker = _repo(tmp_path)
    _plant_worktree_filter(repo, tmp_path, marker)

    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(repo), "add", "-A"],
                   capture_output=True, timeout=60, env=safe_git.safe_env())

    assert marker.exists(), "the planted filter did not run even without the check; the cases below prove nothing"


def test_a_repo_git_commit_refuses_a_filter_set_in_config_worktree(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _plant_worktree_filter(repo, tmp_path, marker)
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = GitRunner(root=repo).commit(message="planted")

    assert result.ok is False
    assert not marker.exists(), "a repo.git commit ran the filter named in config.worktree"
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert safe_git.executable_local_config(repo) == ["filter.planted.clean"]


def test_workspace_git_status_and_diff_refuse_a_filter_set_in_config_worktree(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _plant_worktree_filter(repo, tmp_path, marker)

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({}, workspace_root=repo)

    assert status["status"] == "refused_repository_config"
    assert "filter.planted.clean" in status["response_text"]
    assert diff["status"] == "refused_repository_config"
    assert not marker.exists(), "git ran the filter named in config.worktree"


def test_an_include_in_config_worktree_is_followed_and_refused(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    included = tmp_path / "included.cfg"
    subprocess.run(["git", "config", "-f", str(included), "filter.planted.clean", _planted_program(tmp_path, marker)],
                   check=True, capture_output=True)
    _git(repo, "config", "--worktree", "include.path", included.as_posix())
    (repo / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=repo).commit(message="planted")

    assert result.ok is False
    assert not marker.exists(), "a filter included from config.worktree ran"
    assert set(safe_git.executable_local_config(repo)) == {"filter.planted.clean", "include.path"}


def test_a_linked_worktrees_own_config_worktree_is_checked_there(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked))
    _git(linked, "config", "--worktree", "filter.planted.clean", _planted_program(tmp_path, marker))
    (linked / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (linked / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=linked).commit(message="planted")

    assert result.ok is False
    assert not marker.exists(), "a linked worktree's config.worktree filter ran"
    # The main checkout does not read the linked worktree's file and keeps working.
    assert "filter.planted.clean" not in safe_git.executable_local_config(repo)
    (repo / "notes.txt").write_text("three\n", encoding="utf-8")
    assert GitRunner(root=repo).commit(message="main still commits").ok is True


def test_a_plain_repository_with_worktree_config_on_still_commits(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", "--worktree", "core.sparseCheckout", "false")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=repo).commit(message="ordinary")

    assert result.ok is True, result.to_dict()
    assert safe_git.executable_local_config(repo) == []


def test_the_check_still_fails_outside_a_repository(tmp_path: Path) -> None:
    # Callers treat this as "could not be checked" and refuse.
    with pytest.raises(RuntimeError):
        safe_git.executable_local_config(tmp_path)


def _superproject(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "upstream").mkdir()
    upstream, _ = _repo(tmp_path / "upstream")
    superproject, marker = _repo(tmp_path)
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
    _git(superproject, "commit", "-q", "-m", "add sub")
    return superproject, superproject / "sub", marker


@pytest.mark.parametrize("scope", ["--local", "--worktree"])
def test_a_filter_in_a_submodules_config_is_refused_in_the_superproject(tmp_path: Path, scope: str) -> None:
    superproject, sub, marker = _superproject(tmp_path)
    _git(sub, "config", "extensions.worktreeConfig", "true")
    _git(sub, "config", scope, "filter.planted.clean", _planted_program(tmp_path, marker))
    (sub / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (sub / "notes.txt").write_text("two\n", encoding="utf-8")
    (superproject / "notes.txt").write_text("two\n", encoding="utf-8")
    head = _git(superproject, "rev-parse", "HEAD").strip()

    status = git_status_workspace({}, workspace_root=superproject)
    diff = git_diff_workspace({}, workspace_root=superproject)
    commit = GitRunner(root=superproject).commit(message="planted")

    assert status["status"] == "refused_repository_config"
    assert "sub:filter.planted.clean" in status["response_text"]
    assert diff["status"] == "refused_repository_config"
    assert commit.ok is False and _git(superproject, "rev-parse", "HEAD").strip() == head
    assert not marker.exists(), "git ran a program named in a submodule's config"


def test_control_hardened_status_in_the_superproject_runs_a_submodules_filter(tmp_path: Path) -> None:
    superproject, sub, marker = _superproject(tmp_path)
    _git(sub, "config", "filter.planted.clean", _planted_program(tmp_path, marker))
    (sub / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (sub / "notes.txt").write_text("two\n", encoding="utf-8")

    subprocess.run(safe_git.hardened_argv(superproject, "status", "--porcelain"),
                   capture_output=True, timeout=60, env=safe_git.safe_env())

    assert marker.exists(), "the submodule's filter did not run even without the check; the case above proves nothing"


def test_a_superproject_with_a_plain_submodule_still_commits(tmp_path: Path) -> None:
    superproject, _sub, _marker = _superproject(tmp_path)
    (superproject / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=superproject).commit(message="ordinary")

    assert result.ok is True, result.to_dict()
    assert safe_git.executable_local_config(superproject) == []


def _submodule_without_a_checkout(tmp_path: Path, marker: Path) -> Path:
    # A gitlink bumped in HEAD whose folder is gone; its git folder under .git/modules names a diff
    # driver program for its files.
    superproject, sub, _ = _superproject(tmp_path)
    upstream = tmp_path / "upstream" / "repo"
    (upstream / "notes.txt").write_text("two\n", encoding="utf-8")
    _git(upstream, "commit", "-q", "-a", "-m", "two")
    _git(sub, "pull", "-q")
    _git(superproject, "commit", "-q", "-a", "-m", "bump sub")
    modules = superproject / ".git" / "modules" / "sub"
    _git(modules, "config", "diff.planted.command", _planted_program(tmp_path, marker))
    (modules / "info").mkdir(exist_ok=True)
    (modules / "info" / "attributes").write_text("*.txt diff=planted\n", encoding="utf-8")
    _git(superproject, "config", "diff.submodule", "diff")
    shutil.rmtree(sub)
    return superproject


def test_control_a_submodule_without_a_checkout_diffs_under_its_git_folders_config(tmp_path: Path) -> None:
    superproject = _submodule_without_a_checkout(tmp_path, tmp_path / "MARKER")

    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(superproject), "log", "--no-ext-diff",
                    "--no-textconv", "-p", "-1"], capture_output=True, timeout=60, env=safe_git.safe_env())

    assert (tmp_path / "MARKER").exists(), "the planted driver did not run even without diff.submodule=short"


def test_a_submodule_without_a_checkout_runs_nothing_in_diff_or_the_rules_scans(tmp_path: Path) -> None:
    from core import user_rules

    marker = tmp_path / "MARKER"
    superproject = _submodule_without_a_checkout(tmp_path, marker)

    diff = git_diff_workspace({}, workspace_root=superproject)
    pushed = user_rules._git_text(superproject, "log", "--no-color", "--format=", "-p", "-U0", "HEAD~1..HEAD")
    _git(superproject, "add", "-A")
    staged = user_rules._git_text(superproject, "diff", "--cached", "--no-color", "-U0")

    assert diff["ok"] is True
    assert "sub" in pushed and "sub" in staged
    assert not marker.exists(), "git ran a diff driver named in a submodule's git folder"
