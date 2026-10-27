"""VOOL reads git's output as UTF-8, whatever the console's code page.

git writes paths and URLs as UTF-8 bytes. A ``subprocess.run(..., text=True)`` decodes them in the
locale's encoding, which on Windows without UTF-8 mode (a Python not started by the launcher) is
cp1252, so a folder named ``é`` reads back as ``Ã©`` and the push checks place a different folder
than git writes to. Every git call in the push checks, and in the Windows gate's placing of the repository and its
submodules, therefore names ``encoding="utf-8"`` with
``errors="surrogateescape"``, as the rest of safe_git does, so the bytes git wrote round-trip.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import safe_git
from core import windows_write_gate as gate
from core.repoops import gitops
from core.repoops.gitops import GitRunner

_IDENTITY = {"GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@local",
             "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@local"}


def _git(cwd: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(_IDENTITY, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "-C", str(cwd), "-c", "protocol.file.allow=always", *args],
                   check=True, capture_output=True, timeout=60, env=env)


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch) -> list[dict]:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    calls: list[dict] = []
    real = subprocess.run

    def spy(*args, **kwargs):
        if kwargs.get("capture_output") or kwargs.get("stdout") is not None:
            calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", spy)
    monkeypatch.setattr(safe_git.subprocess, "run", spy)
    marked = tmp_path / "marked é"
    source, dest, library = marked / "project", marked / "dest é.git", marked / "library é"
    for repo in (library, source):
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_bytes(b"seed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
    _git(source, "submodule", "add", "-q", str(library), "lib é")
    _git(source, "commit", "-q", "-m", "submodule")
    _git(marked, "init", "-q", "--bare", "-b", "main", str(dest))
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    runner = GitRunner(root=source)
    assert runner.push_destination_refusal(dest.as_uri()) == ""
    runner.run("status", "--porcelain")
    places = gate._git_places(str(source))
    assert not isinstance(places, str), places
    return calls


def test_every_git_output_read_by_the_push_checks_is_utf8(recorded) -> None:
    text_reads = [kwargs for kwargs in recorded if kwargs.get("text") or kwargs.get("encoding")]
    assert text_reads
    for kwargs in text_reads:
        assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "surrogateescape", kwargs
