"""git follows links in its own git folder, on this platform, and core/git_write_redirects.py names each one.

The Windows write gate refuses repo.git in a repository whose git folders hold a link or a hard
link (tests/test_windows_gate_refuses_git_writes_sent_elsewhere.py), because git's writes would
then land outside the marked folders. This file pins that premise on the platform it runs on, with
the real git binary through VOOL's own GitRunner (the code repo.git runs) and no gate in front:
a commit writes its objects through a link in place of `.git/objects`, its branch through a link in
place of `.git/refs/heads`, and its message into the file a link or hard link at
`.git/COMMIT_EDITMSG` stands for. On Windows the links are junctions (any user can make one) and,
where the account may create them, symbolic links; elsewhere they are symbolic links. Hard links
need no privilege anywhere. For each, `git_write_redirects.refusal` names the entry. Reproduced on
Linux (git 2.43.0) on 2026-10-20; the Windows gauntlet runs this file on Git for Windows.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import fs_links, git_write_redirects
from core.repoops.gitops import GitRunner

_IDENTITY = {"GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@local",
             "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@local"}
#: Junctions exist only on Windows; symbolic links everywhere (on Windows only where the account may make them).
_DIR_LINKS = ("junction", "symlink") if os.name == "nt" else ("symlink",)
_FILE_LINKS = ("hardlink", "symlink")


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(_IDENTITY, GIT_CONFIG_NOSYSTEM="1")
    out = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                         capture_output=True, encoding="utf-8", timeout=60, env=env)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A repository with one commit and one pending file; the user's global git config is out of reach."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    top = tmp_path / "marked" / "project"
    top.mkdir(parents=True)
    (top / "README.md").write_bytes(b"seed\n")
    _git(top, "init", "-q", "-b", "main")
    _git(top, "config", "user.name", "fx")
    _git(top, "config", "user.email", "fx@local")
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "seed")
    (top / "change.txt").write_bytes(b"pending\n")
    return top


def _make_link(kind: str, link: Path, target: Path) -> None:
    if kind == "junction":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    elif kind == "hardlink":
        os.link(target, link)
    else:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:  # Windows without the symbolic-link privilege or Developer Mode
            if os.name != "nt":
                raise
            pytest.skip(f"this Windows account cannot create symbolic links ({exc})")
    if kind != "hardlink":
        assert fs_links.is_link(link), f"{kind} at {link} is not seen as a link"


def _files(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}


def _named(repo: Path) -> str:
    git_dir = str(repo / ".git")
    return git_write_redirects.refusal(git_dir, git_dir, repo)


@pytest.mark.parametrize("kind", _DIR_LINKS)
@pytest.mark.parametrize("folder", ("objects", "refs/heads"))
def test_a_commit_writes_through_a_link_in_place_of_a_git_folder(repo: Path, tmp_path: Path, kind: str, folder: str) -> None:
    outside = tmp_path / "elsewhere" / folder.replace("/", "-")
    outside.parent.mkdir()
    shutil.move(str(repo / ".git" / folder), str(outside))
    _make_link(kind, repo / ".git" / folder, outside)
    before = _files(outside)

    assert f"the repository's git folder holds a link ({folder})" in _named(repo)
    result = GitRunner(root=repo).commit(message="through the link")

    assert result.ok, result.to_dict()
    assert _files(outside) != before, f"git's commit did not write through the {kind} in place of .git/{folder}"


@pytest.mark.parametrize("kind", _FILE_LINKS)
def test_a_commit_overwrites_the_file_a_link_at_commit_editmsg_stands_for(repo: Path, tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_bytes(b"precious\n")
    inner = repo / ".git" / "COMMIT_EDITMSG"
    inner.unlink(missing_ok=True)
    _make_link(kind, inner, precious)

    said = _named(repo)
    if kind == "hardlink":
        assert "a file in the repository's git folder (COMMIT_EDITMSG) is also another file's name" in said
    else:
        assert "the repository's git folder holds a link (COMMIT_EDITMSG)" in said
    result = GitRunner(root=repo).commit(message="through the link")

    assert result.ok, result.to_dict()
    assert precious.read_bytes() != b"precious\n", f"git's commit did not write through the {kind} at COMMIT_EDITMSG"
    assert b"through the link" in precious.read_bytes()


def test_a_repository_without_links_is_not_named(repo: Path) -> None:
    assert _named(repo) == ""
