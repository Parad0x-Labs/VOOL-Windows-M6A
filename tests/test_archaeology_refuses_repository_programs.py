"""Project archaeology refuses a repository whose own config names a program.

Its git probes carried safe_git's command-line hardening, but not the check every other git route
runs first (``safe_git.executable_local_config``). Hardening cannot switch off every key: with
``log.showSignature=true`` and ``gpg.program`` set in the repository's own config, the
``git log`` archaeology runs on a signed commit ran that program (reproduced on f8d2e30). A
repository whose local config defines any program-running key is now refused before git reads it,
and the refusal is disclosed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core import project_archaeology as arch

pytestmark = [
    pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                       reason="git is not installed"),
    pytest.mark.skipif(sys.platform == "win32", reason="the marker program is a POSIX shell script"),
]


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        check=True, capture_output=True, text=True, input=stdin,
    ).stdout.strip()


def _marker(tmp_path: Path) -> tuple[Path, Path]:
    ran = tmp_path / "MARKER-RAN"
    program = tmp_path / "marker.sh"
    program.write_text(f'#!/bin/sh\necho "$@" >> "{ran}"\ncat >/dev/null\nexit 1\n', encoding="utf-8")
    program.chmod(0o755)
    return program, ran


def _signed_history(tmp_path: Path, program: Path) -> tuple[Path, str, str]:
    """Two commits, the second carrying a (bogus) signature, and a repository config that makes
    `git log` verify signatures with a program of the repository's choosing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "notes.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "note 1")
    first = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    body = (
        f"tree {tree}\nparent {first}\n"
        "author t <t@example.test> 1700000000 +0000\n"
        "committer t <t@example.test> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n"
        "\nsigned note\n"
    )
    second = _git(repo, "hash-object", "-t", "commit", "-w", "--stdin", stdin=body)
    _git(repo, "update-ref", "HEAD", second)
    _git(repo, "config", "gpg.program", str(program))
    _git(repo, "config", "log.showSignature", "true")
    return repo, first, second


def test_compare_refuses_a_repository_whose_config_names_a_program(tmp_path) -> None:
    program, ran = _marker(tmp_path)
    repo, first, second = _signed_history(tmp_path, program)

    with pytest.raises(arch.ScopeRefused) as refused:
        arch.compare(kind="git_commits", a=first, b=second, workspace_root=str(repo))

    assert "gpg.program" in refused.value.reason
    assert not ran.exists(), ran.read_text()


def test_recovery_plan_for_a_path_discloses_the_refusal_and_never_runs_the_program(tmp_path) -> None:
    program, ran = _marker(tmp_path)
    repo, _first, _second = _signed_history(tmp_path, program)

    envelope = arch.recovery_plan(path="notes.txt", workspace_root=str(repo))

    assert not ran.exists(), ran.read_text()
    assert not [row for row in envelope["results"] if row.get("store") == "git"], envelope["results"]
    assert any("gpg.program" in reason for reason in envelope["limits"].get("truncation_reasons", [])), envelope


def test_locate_by_commit_never_runs_the_program(tmp_path) -> None:
    program, ran = _marker(tmp_path)
    repo, _first, second = _signed_history(tmp_path, program)

    try:
        arch.locate(second, workspace_root=str(repo))
    except arch.ReferenceNotFound:
        pass
    assert not ran.exists(), ran.read_text()
