"""Off Windows, a chat patch writes only the files VOOL read from it and checked.

`apply_unified_diff_workspace` checks the paths it reads from the `---`/`+++` lines, then hands the
patch to `git apply`, its own Python engine or GNU `patch`. The external engines read the same text
their own way, so the file they write can differ from the one VOOL checked:

* `git apply` drops the first component of every path unless told `-p0`, `a/`/`b/` or not, so a
  plain `src/notes.txt` header changes `notes.txt`;
* both external engines end a header path at a tab (as `diff -u` writes a timestamp after it), and
  VOOL read the tab and what follows as part of the name;
* a git `rename to` / `copy to` line names a destination that has no `---`/`+++` line;
* GNU `patch` was given `-p1` whenever `/dev/null` or ` a/` appeared anywhere in the text.

Here the runtime's protected state is placed at `<workspace>/data`, as when a chat is bound to a folder
that holds the runtime home. Every case must leave it untouched, and the files the result names must be
the files that changed. Renames and copies are refused, as on Windows.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import core.runtime_state_protection as runtime_state_protection
import core.windows_write_gate as gate
from core.execution import workspace_tools
from core.execution.workspace_tools import apply_unified_diff_workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The tool strips the patch's text, final newline included; a closing marker line keeps the last
# hunk line whole, so the external engines read the patch rather than calling it corrupt.
END = "\n\\ No newline at end of file\n"


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


@pytest.fixture(autouse=True)
def off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "data").mkdir()
    (root / "data" / "x.txt").write_text("a\nb\nc\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "f.txt").write_text("one\n", encoding="utf-8")
    (root / "f.txt").write_text("one\n", encoding="utf-8")
    (root / "g.txt").write_text("g\n", encoding="utf-8")
    (root / "ok.txt").write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(runtime_state_protection, "protected_state_roots", lambda: (root / "data",))
    return root


@pytest.fixture()
def without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("patch") is None:
        pytest.skip("needs GNU patch")
    real_which = shutil.which
    monkeypatch.setattr(
        workspace_tools.shutil, "which", lambda cmd, *a, **k: None if cmd == "git" else real_which(cmd, *a, **k)
    )


def _apply(root: Path, patch: str) -> tuple[dict, dict[str, bytes], dict[str, bytes]]:
    before = _files(root)
    result = apply_unified_diff_workspace({"patch": patch.rstrip("\n") + END}, workspace_root=root, session_id="s")
    return result, before, _files(root)


def _assert_lands_only_where_checked(result: dict, before: dict, after: dict) -> None:
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    protected = [p for p in changed if p.startswith("data/")]
    assert protected == [], f"protected state written: {protected} ({result.get('status')})"
    reported = set(result.get("details", {}).get("changed_paths") or [])
    unreported = [p for p in changed if p not in reported]
    assert unreported == [], f"files changed that the result does not name: {unreported} ({result.get('status')})"


GIT_CASES = {
    "plain_headers_lose_a_component": "--- /dev/null\n+++ z/data/new.txt\n@@ -0,0 +1 @@\n+x\n",
    "header_path_ends_at_a_tab": "--- /dev/null\n+++ b/data/new.txt\t/../../ok.txt\n@@ -0,0 +1 @@\n+x\n",
    "rename_section_without_hunks": (
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        "diff --git a/g.txt b/data/new.txt\nsimilarity index 100%\nrename from g.txt\nrename to data/new.txt\n"
    ),
    "copy_section_without_hunks": (
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        "diff --git a/g.txt b/data/new.txt\nsimilarity index 100%\ncopy from g.txt\ncopy to data/new.txt\n"
    ),
}

# VOOL's own engine places a section only at the line it names, so these sections (one line off) reach
# GNU patch, which places them at an offset.
PATCH_CASES = {
    "dev_null_elsewhere_strips_a_plain_section": (
        "--- z/data/x.txt\n+++ z/data/x.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+n\n"
    ),
    "header_path_ends_at_a_tab": "--- data/x.txt\t/../../ok.txt\n+++ data/x.txt\t/../../ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n",
}


@pytest.mark.parametrize("patch", GIT_CASES.values(), ids=GIT_CASES.keys())
def test_git_apply_writes_only_the_checked_files(workspace: Path, patch: str) -> None:
    _assert_lands_only_where_checked(*_apply(workspace, patch))


@pytest.mark.parametrize("patch", PATCH_CASES.values(), ids=PATCH_CASES.keys())
def test_gnu_patch_writes_only_the_checked_files(workspace: Path, without_git: None, patch: str) -> None:
    _assert_lands_only_where_checked(*_apply(workspace, patch))


def test_gnu_patch_leaves_no_backup_file_beside_a_file_it_placed_at_an_offset(workspace: Path, without_git: None) -> None:
    result, before, after = _apply(workspace, "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n")
    assert result["ok"] is True and result["details"]["engine"] == "patch", (result["status"], result["response_text"])
    assert after["ok.txt"] == b"a\nB\nc\n"
    _assert_lands_only_where_checked(result, before, after)


@pytest.mark.parametrize("operation", ["rename", "copy"])
def test_a_rename_or_copy_is_refused_with_nothing_written(workspace: Path, operation: str) -> None:
    patch = (
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        f"diff --git a/g.txt b/h.txt\nsimilarity index 100%\n{operation} from g.txt\n{operation} to h.txt\n"
    )
    result, before, after = _apply(workspace, patch)
    assert result["ok"] is False and result["status"] == "invalid_arguments", (result["status"], result["response_text"])
    assert after == before


# Ordinary patches keep landing where they say.

def test_a_git_style_patch_still_applies_with_git(workspace: Path) -> None:
    result, before, after = _apply(workspace, "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n")
    assert result["ok"] is True and result["details"]["engine"] == "git_apply", (result["status"], result["response_text"])
    assert after["f.txt"] == b"two\n"
    _assert_lands_only_where_checked(result, before, after)


def test_plain_headers_change_the_file_they_name_not_one_a_folder_up(workspace: Path) -> None:
    result, before, after = _apply(workspace, "--- src/f.txt\n+++ src/f.txt\n@@ -1 +1 @@\n-one\n+two\n")
    assert result["ok"] is True, (result["status"], result["response_text"])
    assert after["src/f.txt"] == b"two\n" and after["f.txt"] == b"one\n"
    _assert_lands_only_where_checked(result, before, after)


def test_a_diff_u_timestamp_after_the_name_is_not_part_of_the_name(workspace: Path) -> None:
    patch = (
        "--- f.txt\t2026-10-21 10:00:00.000000000 +0000\n+++ f.txt\t2026-10-21 10:01:00.000000000 +0000\n"
        "@@ -1 +1 @@\n-one\n+two\n"
    )
    result, before, after = _apply(workspace, patch)
    assert result["ok"] is True and result["details"]["changed_paths"] == ["f.txt"], (result["status"], result["response_text"])
    assert after["f.txt"] == b"two\n"
    _assert_lands_only_where_checked(result, before, after)
