"""Which repository settings refuse git depends on the command VOOL runs (core/safe_git.py).

Signing: a repository that switches on signing for its own commits (``commit.gpgSign=true``, common
in a developer's repository) must still be readable: status, diff, log and the user-rules scans
cannot sign or verify. Every command that could sign or check a signature is still refused, and the
user's gpg never runs for it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import safe_git, user_rules
from core.execution.git_tools import git_diff_workspace, git_status_workspace
from core.repoops.gitops import GitRunner

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")

_POSIX_SCRIPT = pytest.mark.skipif(os.name == "nt", reason="the stand-in gpg is a shebang script")


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
    return repo, tmp_path / "MARKER"


def _user_signing_program(tmp_path: Path, monkeypatch, marker: Path) -> None:
    # The user's own gpg.program, in a global config under a private HOME; it leaves the marker.
    script = tmp_path / "fake-gpg"
    script.write_text(
        f"#!{sys.executable}\nimport pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('\\n[GNUPG:] SIG_CREATED D 1 8 00 0 X\\n')\n"
        "sys.stdout.write('-----BEGIN PGP SIGNATURE-----\\n\\nAA==\\n-----END PGP SIGNATURE-----\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "config", "-f", str(home / ".gitconfig"), "gpg.program", script.as_posix()],
                   check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _forged_signature_commit(repo: Path) -> str:
    # A commit carrying a signature header the repository wrote itself, with no key.
    tree = _git(repo, "write-tree").strip()
    body = (f"tree {tree}\nparent {_git(repo, 'rev-parse', 'HEAD').strip()}\n"
            "author T <t@example.test> 1 +0000\ncommitter T <t@example.test> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n AAAA\n -----END PGP SIGNATURE-----\n\nforged\n")
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                         check=True, capture_output=True, text=True).stdout.strip()
    _git(repo, "branch", "forged", sha)
    return sha


def test_a_repository_that_signs_its_own_commits_can_still_be_read(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "tag.gpgSign", "true")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")

    status = git_status_workspace({}, workspace_root=repo)
    diff = git_diff_workspace({"cached": True}, workspace_root=repo)
    staged = user_rules._git_text(repo, "diff", "--cached", "--no-color", "--no-ext-diff", "-U0")
    code, out, err = GitRunner(root=repo).run("log", "-1", "--format=%s")

    assert status["ok"] is True, status
    assert diff["ok"] is True and "+two" in diff["response_text"], diff
    assert "+two" in staged
    assert (code, out.strip()) == (0, "init"), err
    assert not marker.exists()


def test_commands_that_sign_are_still_refused_there_and_the_users_gpg_never_runs(tmp_path: Path,
                                                                                 monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", "commit.gpgSign", "true")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD").strip()

    commit = GitRunner(root=repo).commit(message="signed")
    tag = GitRunner(root=repo).tag("v1", message="signed")

    assert commit.ok is False and tag.ok is False
    assert "commit.gpgsign (set in .git/config)" in commit.stderr, commit.to_dict()
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert _git(repo, "tag", "--list").strip() == ""
    assert not marker.exists(), "a repository's commit.gpgSign made git run the user's gpg"


@pytest.mark.parametrize("argv", [
    ("commit", "-m", "x"), ("tag", "-a", "v1"), ("merge", "main"), ("push", "origin"), ("cherry-pick", "x"),
    ("log", "--show-signature"), ("show", "--show-signature"), ("log", "-S", "x"), ("status", "--gpg-sign"),
    ("merge-base", "--verify-signatures"), ("frobnicate",), (),
])
def test_any_command_that_can_sign_or_verify_keeps_the_signing_keys(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "commit.gpgSign", "true")
    _git(repo, "config", "gpg.format", "ssh")

    found = safe_git.executable_config_for(repo, argv)

    assert found == ["commit.gpgsign (set in .git/config)", "gpg.format (set in .git/config)"], argv


@pytest.mark.parametrize("argv", [("status", "--short"), ("diff", "--cached"), ("log", "-1"), ("show", "HEAD"),
                                  ("rev-parse", "HEAD"), ("for-each-ref",), ("config", "--get", "x.y")])
def test_reads_ignore_signing_keys_but_never_programs(tmp_path: Path, argv: tuple[str, ...]) -> None:
    repo, _marker = _repo(tmp_path)
    for key, value in (("commit.gpgSign", "true"), ("push.gpgSign", "if-asked"), ("gpg.format", "x509"),
                       ("log.showSignature", "true"), ("merge.verifySignatures", "true")):
        _git(repo, "config", key, value)
    _git(repo, "config", "filter.planted.clean", "anything")

    assert safe_git.executable_config_for(repo, argv) == ["filter.planted.clean (set in .git/config)"], argv


@_POSIX_SCRIPT
def test_reading_a_repository_that_turns_signature_checks_on_never_runs_gpg(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _forged_signature_commit(repo)
    _git(repo, "config", "log.showSignature", "true")
    _git(repo, "config", "gpg.format", "openpgp")
    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(repo), "log", "-1", "forged"],
                   capture_output=True, timeout=60, env=safe_git.safe_env())
    assert marker.exists(), "control: the repository's log.showSignature did not run gpg; the case below proves nothing"
    marker.unlink()

    text = user_rules._git_text(repo, "log", "-1", "--no-color", "--format=%B", "forged")
    code, out, _ = GitRunner(root=repo).run("show", "-s", "--format=%B", "forged")

    assert "forged" in text and code == 0 and "forged" in out
    assert not marker.exists(), "VOOL's read ran the user's gpg on a forged signature"


def test_the_refusal_names_where_each_key_is_set(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "filter.a.clean", "x")
    _git(repo, "config", "--worktree", "filter.a.clean", "y")
    _git(repo, "config", "--worktree", "core.sshCommand", "z")

    assert safe_git.executable_config_for(repo, ("status",)) == [
        "core.sshcommand (set in config.worktree)",
        "filter.a.clean (set in .git/config)",
        "filter.a.clean (set in config.worktree)",
    ]
    assert safe_git.executable_local_config(repo) == ["core.sshcommand", "filter.a.clean"]


def test_without_a_command_every_key_still_counts(tmp_path: Path) -> None:
    # The Windows write gate asks GitRunner without a command (windows_write_gate.py), and
    # executable_local_config keeps its bare-key contract (workspace_tools.py, Job 6).
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "commit.gpgSign", "true")

    assert "commit.gpgsign" in GitRunner(root=repo)._repository_refusal(mutating=False)
    assert safe_git.executable_local_config(repo) == ["commit.gpgsign"]


@pytest.mark.parametrize("key, value, safe", [
    ("commit.gpgsign", "false", True), ("Commit.GpgSign", "OFF", True), ("tag.gpgsign", "0", True),
    ("commit.gpgsign", "true", False), ("commit.gpgsign", None, False), ("commit.gpgsign", "", False),
    ("gpg.format", "openpgp", True), ("gpg.format", "ssh", False), ("gpg.format", "OpenPGP", False),
    ("filter.x.clean", "false", False), ("core.sshcommand", "", False),
])
def test_is_safe_value(key: str, value: str | None, safe: bool) -> None:
    assert safe_git.is_safe_value(key, value) is safe
