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

import hashlib
import os
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


def _user_signing_program(tmp_path: Path, monkeypatch, marker: Path) -> None:
    # The user's own gpg.program, in a global config under a private HOME. git execs it directly
    # (no shell), so it is a script with a shebang. It answers git like gpg does, so a signed
    # commit succeeds when signing is the user's choice.
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


_POSIX_SCRIPT = pytest.mark.skipif(os.name == "nt", reason="the stand-in gpg is a shebang script; "
                                                          "prove the signing run on Windows separately")


@_POSIX_SCRIPT
def test_control_hardened_git_runs_the_signing_program_when_the_repository_turns_signing_on(
        tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", "commit.gpgSign", "true")

    subprocess.run([*safe_git.hardened_argv(repo, "commit", "--allow-empty", "-q", "-m", "signed")],
                   capture_output=True, timeout=60, env=safe_git.safe_env())

    assert marker.exists(), "git did not run the signing program; the cases below prove nothing"


@pytest.mark.parametrize("key", ["commit.gpgSign", "tag.gpgSign", "tag.forceSignAnnotated"])
def test_signing_switched_on_by_the_repository_is_refused_and_never_runs(tmp_path: Path, monkeypatch,
                                                                        key: str) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _git(repo, "config", key, "true")
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD").strip()

    commit = GitRunner(root=repo).commit(message="signed")
    tag = GitRunner(root=repo).tag("v1", message="signed")

    assert commit.ok is False and tag.ok is False
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert key.lower() in safe_git.executable_local_config(repo)
    assert not marker.exists(), f"a repository's {key} made git run the signing program"


@_POSIX_SCRIPT
def test_signing_the_user_turned_on_is_still_the_users_choice(tmp_path: Path, monkeypatch) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    subprocess.run(["git", "config", "-f", str(tmp_path / "home" / ".gitconfig"), "commit.gpgSign", "true"],
                   check=True, capture_output=True)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=repo).commit(message="signed by the user's choice")

    assert result.ok is True, result.to_dict()
    assert marker.exists()


@pytest.mark.parametrize("key", ["lfs.customtransfer.planted.path", "lfs.customtransfer.planted.args",
                                 "lfs.standalonetransferagent"])
def test_a_git_lfs_transfer_agent_set_by_the_repository_is_refused(tmp_path: Path, key: str) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", key, _planted_program(tmp_path, marker))

    status = git_status_workspace({}, workspace_root=repo)

    assert status["status"] == "refused_repository_config", key
    assert key.lower() in status["response_text"]
    assert not marker.exists()


@pytest.mark.parametrize("value", ["false", "FALSE", "No", "off", "0"])
def test_a_repository_that_switches_signing_off_still_commits(tmp_path: Path, monkeypatch, value: str) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    subprocess.run(["git", "config", "-f", str(tmp_path / "home" / ".gitconfig"), "commit.gpgSign", "true"],
                   check=True, capture_output=True)
    for key in ("commit.gpgSign", "tag.gpgSign", "tag.forceSignAnnotated", "push.gpgSign"):
        _git(repo, "config", key, value)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")

    result = GitRunner(root=repo).commit(message="unsigned by the repository's choice")

    assert safe_git.executable_local_config(repo) == []
    assert result.ok is True, result.to_dict()
    assert not marker.exists(), "signing switched off by the repository still ran the signing program"


@pytest.mark.parametrize("raw", [
    "[commit]\n\tgpgSign\n",                   # a bare key is true
    "[commit]\n\tgpgSign =\n",                 # empty
    "[commit]\n\tgpgSign = 00\n",              # git reads it as false; not a definite spelling here
    "[commit]\n\tgpgSign = \" false\"\n",      # quoted leading space
    "[commit]\n\tgpgSign = 1\n",
    "[commit]\n\tgpgSign = falsey\n",
    "[push]\n\tgpgSign = if-asked\n",
    "[tag]\n\tgpgSign = false\n\tgpgSign = true\n",   # every value must be false
])
def test_signing_values_that_are_not_a_definite_false_are_refused(tmp_path: Path, raw: str) -> None:
    repo, _marker = _repo(tmp_path)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(raw)

    assert safe_git.executable_local_config(repo), raw
    assert GitRunner(root=repo).commit(message="x").ok is False


def test_signing_switched_on_in_config_worktree_is_refused_even_when_local_says_false(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "--worktree", "commit.gpgSign", "true")

    assert safe_git.executable_local_config(repo) == ["commit.gpgsign"]


@pytest.mark.parametrize("key", ["lfs.customtransfer.planted.path", "lfs.standalonetransferagent"])
def test_a_git_lfs_transfer_agent_is_refused_even_with_a_false_looking_value(tmp_path: Path, key: str) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", key, "false")

    assert safe_git.executable_local_config(repo) == [key]


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


@pytest.mark.parametrize("key", ["log.showSignature", "merge.verifySignatures"])
def test_signature_checks_switched_on_by_the_repository_are_refused(tmp_path: Path, monkeypatch, key: str) -> None:
    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    _forged_signature_commit(repo)
    _git(repo, "config", key, "true")

    merge = GitRunner(root=repo).merge("forged")

    assert merge.ok is False
    assert safe_git.executable_local_config(repo) == [key.lower()]
    assert not marker.exists(), f"a repository's {key} made git run the user's gpg"


@_POSIX_SCRIPT
def test_vool_log_never_verifies_signatures_even_when_the_user_turned_it_on(tmp_path: Path, monkeypatch) -> None:
    from core import user_rules

    repo, marker = _repo(tmp_path)
    _user_signing_program(tmp_path, monkeypatch, marker)
    subprocess.run(["git", "config", "-f", str(tmp_path / "home" / ".gitconfig"), "log.showSignature", "true"],
                   check=True, capture_output=True)
    _forged_signature_commit(repo)

    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(repo), "log", "-1", "forged"],
                   capture_output=True, timeout=60, env=safe_git.safe_env())
    assert marker.exists(), "control: git log did not run the signature check; the case below proves nothing"
    marker.unlink()

    text = user_rules._git_text(repo, "log", "-1", "--no-color", "--format=%B", "forged")

    assert "forged" in text
    assert not marker.exists(), "VOOL's git log ran the signature check on a forged signature"


@pytest.mark.parametrize("value", ["ssh", "x509", "OpenPGP", ""])
def test_a_repository_cannot_choose_another_signing_format(tmp_path: Path, value: str) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "gpg.format", value)

    assert safe_git.executable_local_config(repo) == ["gpg.format"]


def test_the_default_signing_format_set_by_the_repository_passes(tmp_path: Path) -> None:
    repo, _marker = _repo(tmp_path)
    _git(repo, "config", "gpg.format", "openpgp")
    _git(repo, "config", "log.showSignature", "false")
    _git(repo, "config", "merge.verifySignatures", "no")

    assert safe_git.executable_local_config(repo) == []


def _submodule_rebuilt_on_switch(tmp_path: Path, marker: Path) -> tuple[Path, str]:
    # Branch `side` moves the gitlink to a submodule commit whose files use a filter; the submodule
    # folder is empty on main, and its git folder under .git/modules names that filter's program.
    superproject, sub, _ = _superproject(tmp_path)
    upstream = tmp_path / "upstream" / "repo"
    (upstream / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
    (upstream / "notes.txt").write_text("filtered\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "filtered")
    _git(superproject, "switch", "-q", "-c", "side")
    _git(sub, "pull", "-q")
    _git(superproject, "commit", "-q", "-a", "-m", "bump sub")
    _git(superproject, "switch", "-q", "-")
    shutil.rmtree(sub)
    sub.mkdir()
    modules = superproject / ".git" / "modules" / "sub"
    _git(modules, "config", "filter.planted.smudge", _planted_program(tmp_path, marker))
    _git(modules, "config", "filter.planted.clean", _planted_program(tmp_path, marker))
    _git(superproject, "config", "submodule.recurse", "true")
    return superproject, _git(superproject, "rev-parse", "side").strip()


def test_control_a_recursive_switch_rebuilds_a_submodule_and_runs_its_git_folders_filter(tmp_path: Path) -> None:
    marker = tmp_path / "MARKER"
    superproject, side = _submodule_rebuilt_on_switch(tmp_path, marker)

    subprocess.run(["git", *safe_git.hardening_args(), "-C", str(superproject), "switch", "-c", "control", side],
                   capture_output=True, timeout=60, env=safe_git.safe_env())

    assert marker.exists(), "the recursive switch did not run the filter; the case below proves nothing"


def test_a_branch_switch_never_rebuilds_a_submodule_from_its_git_folder(tmp_path: Path) -> None:
    marker = tmp_path / "MARKER"
    superproject, side = _submodule_rebuilt_on_switch(tmp_path, marker)

    result = GitRunner(root=superproject).create_branch("next", start_point=side)

    assert result.ok is True, result.to_dict()
    assert not marker.exists(), "a recursive switch ran a filter named in a submodule's git folder"


def _auto_gc_runs_the_recent_objects_hook(tmp_path: Path, marker: Path) -> Path:
    # The repository makes its next commit's automatic gc prune at once, and names a program for
    # gc to ask which objects are recent. Two loose unreachable objects in objects/17 trip gc.auto=1.
    repo, _ = _repo(tmp_path)
    found = 0
    for index in range(100000):
        content = f"loose {index}\n".encode()
        if hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest().startswith("17"):
            subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                           check=True, capture_output=True)
            found += 1
            if found == 2:
                break
    for key, value in (("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.pruneExpire", "now"),
                       ("gc.recentObjectsHook", _planted_program(tmp_path, marker))):
        _git(repo, "config", key, value)
    (repo / "notes.txt").write_text("two\n", encoding="utf-8")
    return repo


def test_control_a_commits_automatic_gc_runs_the_repositorys_recent_objects_hook(tmp_path: Path) -> None:
    marker = tmp_path / "MARKER"
    repo = _auto_gc_runs_the_recent_objects_hook(tmp_path, marker)

    subprocess.run(safe_git.hardened_argv(repo, "commit", "-q", "-a", "-m", "gc"), capture_output=True,
                   timeout=60, env=safe_git.safe_env())

    assert marker.exists(), "the commit's gc did not run the hook; the case below proves nothing"


def test_a_repositorys_recent_objects_hook_is_refused_and_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "MARKER"
    repo = _auto_gc_runs_the_recent_objects_hook(tmp_path, marker)

    result = GitRunner(root=repo).commit(message="gc")

    assert result.ok is False
    assert safe_git.executable_local_config(repo) == ["gc.recentobjectshook"]
    assert not marker.exists(), "a commit's automatic gc ran the repository's gc.recentObjectsHook"


@pytest.mark.parametrize("key", [
    "gc.recentObjectsHook", "uploadpack.packObjectsHook", "gpg.ssh.defaultKeyCommand", "remote.origin.vcs",
    "interactive.diffFilter", "sendemail.sendmailCmd", "hook.planted.command", "hook.planted.event",
    "difftool.planted.path", "mergetool.planted.path",
])
def test_every_program_naming_key_from_the_config_sweep_is_refused(tmp_path: Path, key: str) -> None:
    repo, marker = _repo(tmp_path)
    _git(repo, "config", key, _planted_program(tmp_path, marker))

    status = git_status_workspace({}, workspace_root=repo)

    assert status["status"] == "refused_repository_config", key
    assert key.lower() in status["response_text"]


def test_advice_toggles_named_like_hooks_are_not_programs() -> None:
    assert not safe_git.is_executable_key("advice.ignoredHook")
    assert not safe_git.is_executable_key("core.fsmonitorHookVersion")


def test_aliases_named_like_programs_are_not_refused() -> None:
    # An alias runs only when invoked by name and VOOL never invokes one (module docstring).
    assert not safe_git.is_executable_key("alias.subcmd")
    assert not safe_git.is_executable_key("alias.prehook")
    assert not safe_git.is_executable_key("alias.lastcommand")
