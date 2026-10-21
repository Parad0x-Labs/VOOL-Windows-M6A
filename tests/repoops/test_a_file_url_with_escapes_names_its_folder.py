"""A `file://` push URL with percent escapes names the folder git decodes it to.

git percent-decodes a `file://` URL before it reads the path (connect.c `url_decode`), and
`Path.as_uri()` escapes every space, so a destination in a folder such as `C:\\Users\\Jo Smith` is
written `file:///C:/Users/Jo%20Smith/...`. VOOL refused every escaped URL ("cannot tell which
folder"), so such a push never landed, and a push outside the marked folders was refused for the
wrong reason. VOOL now decodes the URL once, as git does, and places the folder it names; a form
the two could read differently (an escaped NUL, slash or backslash, a malformed escape, bytes that are
not UTF-8, which git keeps as raw bytes and Python would turn into U+FFFD, or a decoded path that starts with two slashes, which Windows reads as a share) is still refused. The
Windows decision is switched on through the gate's `_IS_WINDOWS`, so the file runs on every platform.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.repoops.gitops import GitRunner

_IDENTITY = {"GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@local",
             "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@local"}


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(_IDENTITY, GIT_CONFIG_NOSYSTEM="1")
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8", timeout=60, env=env)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A marked folder holding the pushing repository, and an unmarked folder beside it, both with a space."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    marked, elsewhere = tmp_path / "marked folder", tmp_path / "else where"
    source = marked / "project"
    source.mkdir(parents=True)
    elsewhere.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_bytes(b"seed\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "seed")
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    return {"marked": marked, "elsewhere": elsewhere, "source": source}


def _bare(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True, timeout=60)
    return path


def _push(source: Path, remote: str) -> tuple[int, str]:
    runner = GitRunner(root=source)
    code, _, err = runner.run(*runner.push_argv(remote=remote, ref="main", sha=runner.head()))
    return code, err


def _refs(git_dir: Path) -> str:
    return _git(git_dir, "for-each-ref", "--format=%(refname) %(objectname)")


def test_a_push_to_an_escaped_file_url_inside_the_marked_folders_lands(world) -> None:
    destination = _bare(world["marked"] / "dest repo.git")
    url = destination.as_uri()
    assert "%20" in url

    assert GitRunner(root=world["source"]).push_destination_refusal(url) == ""
    code, err = _push(world["source"], url)

    assert code == 0, err
    assert _git(destination, "rev-parse", "refs/heads/main").strip() == _git(world["source"], "rev-parse", "HEAD").strip()


def test_a_push_to_an_escaped_file_url_outside_the_marked_folders_is_refused_as_outside(world) -> None:
    destination = _bare(world["elsewhere"] / "dest repo.git")
    url = destination.as_uri()
    before = _refs(destination)

    refusal = GitRunner(root=world["source"]).push_destination_refusal(url)
    code, err = _push(world["source"], url)

    assert "which is outside them" in refusal, refusal
    assert code == -1 and "which is outside them" in err, err
    assert _refs(destination) == before


@pytest.mark.parametrize("escape", ("%00", "%2F", "%2f", "%5C", "%zz", "%2", "%ff", "%C3%28", "%ED%A0%80"))
def test_an_escape_git_and_vool_could_read_differently_is_still_refused(world, escape: str) -> None:
    destination = _bare(world["marked"] / "dest.git")
    url = destination.as_uri() + "/x" + escape

    refusal = GitRunner(root=world["source"]).push_destination_refusal(url)

    assert "cannot tell which folder" in refusal, refusal


def test_an_escaped_second_slash_that_would_name_a_share_is_refused(world) -> None:
    refusal = GitRunner(root=world["source"]).push_destination_refusal("file:///%2Fhost/share/r.git")

    assert "cannot tell which folder" in refusal, refusal
