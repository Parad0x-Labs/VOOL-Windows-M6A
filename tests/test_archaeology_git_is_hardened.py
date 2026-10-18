"""Project archaeology's git probes are hardened like every other git route.

Its probe ran `git -C <workspace>` bare: the one git route in production dispatch that
core/safe_git.py did not cover. Its read-only verbs did not run a planted program in a probe on
this branch, so this pins the invariant, not a reproduced exploit: every archaeology git call
carries safe_git's hardening and a scrubbed environment, and a plain repository still compares.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core import project_archaeology as arch
from core import safe_git

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _history(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for n in (1, 2):
        (repo / "notes.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(repo, "add", "notes.txt")
        _git(repo, "commit", "-q", "-m", f"note {n}")
    return repo, _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")


def test_every_archaeology_git_call_is_hardened_and_a_plain_repository_still_compares(tmp_path, monkeypatch) -> None:
    repo, a, b = _history(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='planted'")
    calls: list[tuple[list[str], dict | None]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and Path(str(argv[0])).name.lower().startswith("git"):
            calls.append(([str(part) for part in argv], kwargs.get("env")))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(arch.subprocess, "run", recording_run)
    result = arch.compare(kind="git_commits", a=a, b=b, workspace_root=str(repo))

    assert "notes.txt" in str(result)
    assert calls, "compare ran no git probe"
    hardening = safe_git.hardening_args()
    for argv, env in calls:
        assert argv[1:1 + len(hardening)] == hardening, argv
        assert env is not None and "GIT_CONFIG_PARAMETERS" not in env, "a GIT_* override reached git"
