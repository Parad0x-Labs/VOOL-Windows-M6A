"""Generic file tools never write a repository's git metadata.

A file a tool writes under ``.git`` is a program git later runs for the user: a hook on their next
own ``git commit`` (Git for Windows runs it whatever its mode), ``core.fsmonitor`` in ``.git/config``
on their next ``git status``. Reproduced 20 Oct 2026: workspace.write_file, ensure_directory and
apply_unified_diff wrote ``.git/hooks/pre-commit`` and ``.git/config`` on POSIX and through the
Windows write gate with the workspace flagged. The refusal lives in core.runtime_state_protection,
which every generic writer asks (core.git_metadata_protection).

Each case drives the real dispatch and reads the disk back, independent of the reported status.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core import git_metadata_protection as gm
from core import windows_write_gate as gate


@pytest.fixture(params=["posix", "windows_gate"])
def repo(request, tmp_path, monkeypatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    if request.param == "windows_gate":
        monkeypatch.setattr(gate, "_IS_WINDOWS", True)
        monkeypatch.setenv(gate.ENV_VAR, str(root))
    return root.resolve()


def _dispatch(intent: str, arguments: dict, root: Path):
    from core.runtime_execution_tools import _dispatch_runtime_tool

    return _dispatch_runtime_tool(intent, arguments, source_context={"workspace_root": str(root), "workspace": str(root),
                                                                     "session_id": "git-metadata", "operating_mode": "auto"})


@pytest.mark.parametrize("relative", [
    ".git/hooks/pre-commit", ".git/config", ".git/info/attributes", ".git/modules/sub/config",
    ".GIT/hooks/pre-commit", "sub/.git",
])
def test_the_write_tool_refuses_git_metadata_and_writes_nothing(repo, relative) -> None:
    before = (repo / ".git" / "config").read_bytes()

    result = _dispatch("workspace.write_file", {"path": relative, "content": "#!/bin/sh\necho ran\n"}, repo)

    assert result is not None and result.ok is False
    assert not (repo / relative).exists() or relative == ".git/config"
    assert (repo / ".git" / "config").read_bytes() == before
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_making_a_folder_inside_git_metadata_is_refused(repo) -> None:
    result = _dispatch("workspace.ensure_directory", {"path": ".git/hooks/pre-commit.d"}, repo)

    assert result is not None and result.ok is False
    assert not (repo / ".git" / "hooks" / "pre-commit.d").exists()


def test_a_patch_creating_a_hook_is_refused_before_any_engine(repo) -> None:
    patch = "--- /dev/null\n+++ b/.git/hooks/pre-commit\n@@ -0,0 +1,2 @@\n+#!/bin/sh\n+echo ran\n"

    result = _dispatch("workspace.apply_unified_diff", {"patch": patch}, repo)

    assert result is not None and result.ok is False
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_a_symlink_into_git_metadata_is_refused(repo) -> None:
    (repo / "hooks-link").symlink_to(repo / ".git" / "hooks")

    result = _dispatch("workspace.write_file", {"path": "hooks-link/pre-commit", "content": "x"}, repo)

    assert result is not None and result.ok is False
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_a_bare_repository_inside_the_workspace_is_refused(repo) -> None:
    subprocess.run(["git", "init", "-q", "--bare", str(repo / "mirror.git")], check=True, timeout=60)

    result = _dispatch("workspace.write_file", {"path": "mirror.git/hooks/pre-receive", "content": "x"}, repo)

    assert result is not None and result.ok is False
    assert not (repo / "mirror.git" / "hooks" / "pre-receive").exists()


def test_ordinary_files_beside_git_metadata_are_still_written(repo) -> None:
    for relative in (".gitignore", ".gitattributes", "docs/.github/notes.md", "git/config", ".git-blame-ignore-revs"):
        result = _dispatch("workspace.write_file", {"path": relative, "content": "ok\n"}, repo)
        assert result is not None and result.ok, (relative, result.response_text if result else None)
        assert (repo / relative).read_text(encoding="utf-8") == "ok\n"


@pytest.mark.parametrize("name, metadata", [
    (".git", True), (".GIT", True), (".git.", True), (".git ", True), (".git::$INDEX_ALLOCATION", True),
    ("GIT~1", True), ("git~2", True),
    (".gitignore", False), (".github", False), ("git", False), ("x.git", False), ("..git", False),
])
def test_git_metadata_names_are_read_the_way_the_filesystems_read_them(name, metadata) -> None:
    assert gm.is_git_metadata_name(name) is metadata
