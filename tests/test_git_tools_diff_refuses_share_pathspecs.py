"""workspace.git_diff never hands git a path that names a Windows share, magic or not.

On Windows git resolves an absolute pathspec (prefix_path_gently -> strbuf_realpath), and a lookup
of `\\\\host\\share` is an SMB connection (and an NTLM hash sent to whoever answers). The tool-call
layer refuses share spellings in `path` (windows_path_policy.argument_refusal), but pathspec magic
hides one: ":(literal)//host/s/x" is spelled like no share, and git strips the magic and resolves
the rest. git_diff_workspace now refuses both itself, before git runs.

A plain relative path is still diffed as before.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.execution.git_tools import git_diff_workspace
from tests._nonlocal_touch_recorder import recorder, windows_host

SHARES = ["\\\\attkr9\\s\\x", "//attkr9/s/x", "\\\\?\\UNC\\attkr9\\s\\x"]
MAGIC = [":(literal)//attkr9/s/x", ":(glob)\\\\attkr9\\s\\x", ":!//attkr9/s/x"]
# Plain ids: a test id naming the marker host would reach every process through PYTEST_CURRENT_TEST.
SHARE_IDS = ["unc", "forward-slashes", "long-unc"]
MAGIC_IDS = ["literal", "glob", "exclude"]


def _git(root: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "n@x")
    _git(root, "config", "user.name", "Ninja")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


@pytest.mark.parametrize("cached", [False, True], ids=["worktree", "cached"])
@pytest.mark.parametrize("path", SHARES + MAGIC, ids=SHARE_IDS + MAGIC_IDS)
def test_a_diff_of_a_share_pathspec_is_refused_before_git_runs(repo, recorder, windows_host, path,
                                                               cached) -> None:
    windows_host.on()

    result = git_diff_workspace({"path": path, "cached": cached}, workspace_root=repo)

    assert result["ok"] is False and result["status"] == "refused_network_path", result
    assert recorder.touches == [] and recorder.marker_popen == [], recorder.report()


def test_a_plain_path_is_still_diffed(repo, windows_host) -> None:
    windows_host.on()
    (repo / "a.txt").write_text("two\n")

    result = git_diff_workspace({"path": "a.txt"}, workspace_root=repo)

    assert result["ok"] is True, result
    assert "+two" in result["response_text"] or "+two" in str(result.get("details")), result
