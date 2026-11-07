"""VOOL's own git never adopts a bare repository a job planted in a folder it works in.

A bare repository is just a folder holding HEAD, objects/, refs/ and a config. Git discovers one
in the current folder before it looks for a parent's .git, so a job that can write a workspace
folder could plant one there and have VOOL's next `git -C <that folder>` read the planted config
and refs instead of the real repository's. ``safe.bareRepository=explicit`` makes git use a bare
repository only when it is named with --git-dir, so discovery refuses the planted one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core import safe_git

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def _plain_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=safe_git.safe_env())


def _plant_bare(folder: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(folder)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(folder), "config", "vool.marker", "planted"], check=True)
    subprocess.run(["git", "--git-dir", str(folder), "config", "core.sshCommand", "planted-ssh"], check=True)
    return folder


def _hardened(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(safe_git.hardened_argv(cwd, *args), capture_output=True, text=True, env=safe_git.safe_env())


def test_hardening_names_bare_repositories_explicit_only() -> None:
    args = safe_git.hardening_args()
    assert "safe.bareRepository=explicit" in [args[i + 1] for i, a in enumerate(args) if a == "-c"]


def test_a_planted_bare_repository_is_not_discovered(tmp_path) -> None:
    planted = _plant_bare(tmp_path / "planted")
    # Control: git's default discovery does adopt it, so the refusal below is the setting's doing.
    assert _plain_git(planted, "config", "--get", "vool.marker").stdout.strip() == "planted"
    probe = _hardened(planted, "config", "--get", "vool.marker")
    assert probe.returncode != 0
    assert "planted" not in probe.stdout


def test_a_bare_repository_planted_inside_a_work_tree_is_not_adopted(tmp_path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "vool.marker", "real"], check=True)
    planted = _plant_bare(repo / "sub")
    assert _plain_git(planted, "config", "--get", "vool.marker").stdout.strip() == "planted"
    probe = _hardened(planted, "config", "--get", "vool.marker")
    assert "planted" not in probe.stdout
    # The real repository is untouched and still works through the same hardened door.
    assert _hardened(repo, "config", "--get", "vool.marker").stdout.strip() == "real"


def test_the_config_and_hook_probes_fail_closed_on_a_planted_bare_repository(tmp_path) -> None:
    planted = _plant_bare(tmp_path / "planted")
    with pytest.raises(RuntimeError):
        safe_git.executable_local_config(planted)
    with pytest.raises(RuntimeError):
        safe_git.active_hooks(planted)


def test_an_ordinary_repository_still_works(tmp_path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    probe = _hardened(repo, "rev-parse", "--is-inside-work-tree")
    assert probe.returncode == 0 and probe.stdout.strip() == "true"
    assert safe_git.executable_local_config(repo) == []
    assert safe_git.active_hooks(repo) == []


def test_a_bare_repository_named_explicitly_is_still_checked_for_hooks(tmp_path) -> None:
    # A push destination is a git folder VOOL resolved itself and names with --git-dir; its hooks
    # must still be seen, so the push is refused rather than the check silently skipped.
    destination = _plant_bare(tmp_path / "dest.git")
    hook = destination / "hooks" / "post-receive"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert safe_git.active_hooks(destination, git_dir=True) == ["post-receive"]
