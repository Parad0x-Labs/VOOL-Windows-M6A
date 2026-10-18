"""A repository's own git config and hooks must never make VOOL's git tools run a program.

Reproduced 2026-10-18: `workspace.git_status` on a repository whose .git/config set
`core.fsmonitor` to a script ran that script, unsandboxed and without approval. Each case below
plants a program that would leave a marker file if git ran it; the tools must refuse or
neutralise it and the marker must never appear. A plain repository keeps working.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.execution.git_tools import git_diff_workspace, git_status_workspace, git_summary_workspace

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                   check=True, capture_output=True)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, tmp_path / "MARKER"


def _planted_program(tmp_path: Path, marker: Path) -> str:
    script = tmp_path / "planted.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    python = sys.executable.replace("\\", "/")
    return f'"{python}" "{script.as_posix()}"'


def test_fsmonitor_in_repository_config_is_refused_and_never_runs(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", "core.fsmonitor", _planted_program(tmp_path, marker))

    status = git_status_workspace({}, workspace_root=repo)
    summary = git_summary_workspace({}, workspace_root=repo)

    assert status["ok"] is False and status["status"] == "refused_repository_config"
    assert "core.fsmonitor" in status["response_text"]
    assert summary["status"] == "refused_repository_config"
    assert not marker.exists(), "git ran the repository's fsmonitor program"


def test_a_clean_filter_in_repository_config_is_refused_and_never_runs(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", "filter.planted.clean", _planted_program(tmp_path, marker))
    (repo / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({}, workspace_root=repo)

    assert status["status"] == "refused_repository_config"
    assert diff["status"] == "refused_repository_config"
    assert not marker.exists(), "git ran the repository's clean filter"


def test_a_textconv_driver_in_repository_config_is_refused_and_never_runs(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", "diff.planted.textconv", _planted_program(tmp_path, marker))
    (repo / ".gitattributes").write_text("*.txt diff=planted\n", encoding="utf-8")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    diff = git_diff_workspace({}, workspace_root=repo)

    assert diff["status"] == "refused_repository_config"
    assert not marker.exists(), "git ran the repository's textconv driver"


def test_a_planted_hook_never_runs_when_status_reads_the_index(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    os.utime(repo / "notes.txt", None)

    status = git_status_workspace({}, workspace_root=repo)

    assert status["ok"] is True and "notes.txt" in status["response_text"]
    assert not marker.exists(), "git ran a hook planted in the repository"


@pytest.mark.parametrize("key", [
    "remote.origin.uploadpack", "remote.origin.receivepack", "core.alternateRefsCommand",
    "core.askPass", "sequence.editor", "pager.status", "credential.https://example.test.helper",
    "difftool.x.cmd", "submodule.sub.update", "protocol.ext.allow",
])
def test_every_program_running_key_kind_in_repository_config_is_refused(tmp_path: Path, key: str) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", key, _planted_program(tmp_path, marker))

    status = git_status_workspace({}, workspace_root=repo)

    assert status["status"] == "refused_repository_config", key
    assert not marker.exists()


def test_inherited_git_config_environment_cannot_inject_a_program(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    (repo / ".gitattributes").write_text("*.txt filter=injected\n", encoding="utf-8")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.injected.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", _planted_program(tmp_path, marker))

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({}, workspace_root=repo)

    assert status["ok"] is True and diff["ok"] is True
    assert not marker.exists(), "an inherited GIT_CONFIG_* variable made git run a program"


def test_ordinary_repository_settings_are_not_treated_as_programs() -> None:
    from core import safe_git

    for key in ("user.name", "remote.origin.url", "remote.origin.fetch", "core.autocrlf", "core.excludesfile",
                "filter.lfs.required", "branch.main.remote", "lfs.repositoryformatversion", "submodule.sub.path",
                "submodule.sub.url", "alias.st"):
        assert not safe_git.is_executable_key(key), key
    for key in ("core.fsmonitor", "filter.x.process", "diff.x.textconv", "merge.x.driver", "gpg.ssh.program",
                "includeIf.gitdir:/x/.path", "include.path"):
        assert safe_git.is_executable_key(key), key


def test_a_plain_repository_still_reports_status_and_diff(tmp_path: Path) -> None:
    repo, marker = _repo(tmp_path)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({}, workspace_root=repo)

    assert status["ok"] is True and status["status"] == "executed" and "notes.txt" in status["response_text"]
    assert diff["ok"] is True and "+two" in diff["response_text"]
