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
from core import safe_git
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
    # Off Windows git runs: core/safe_git.py decides that from the platform itself, so on a Windows
    # host the gate's seam alone would leave git refused (as tests/test_windows_gate_checks_where_git_writes.py
    # `_as_before_0_7_1` does, turn both off).
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Written as bytes: in text mode Windows would write each "\n" as "\r\n", which VOOL refuses to
    # patch (a carriage return it could not keep), so no case here would reach an engine there.
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "data").mkdir()
    (root / "data" / "x.txt").write_bytes(b"a\nb\nc\n")
    (root / "src").mkdir()
    (root / "src" / "f.txt").write_bytes(b"one\n")
    (root / "f.txt").write_bytes(b"one\n")
    (root / "g.txt").write_bytes(b"g\n")
    (root / "ok.txt").write_bytes(b"a\nb\nc\n")
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


# A git `copy to` section beside an ordinary edit named a hook no `---`/`+++` line names; with the mode
# lines the copy is executable, and the user's own next `git commit` runs it.
HOOK_COPY = (
    "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
    "diff --git a/h.sh b/.git/hooks/pre-commit\nold mode 100644\nnew mode 100755\nsimilarity index 100%\n"
    "copy from h.sh\ncopy to .git/hooks/pre-commit\n"
)


@pytest.mark.parametrize("engines", ["git_present", "without_git"])
def test_a_copy_section_does_not_write_a_git_hook(
    workspace: Path, request: pytest.FixtureRequest, engines: str
) -> None:
    if engines == "without_git":
        request.getfixturevalue("without_git")
    (workspace / "h.sh").write_bytes(b"#!/bin/sh\ntouch HOOK_RAN\n")
    hook = workspace / ".git" / "hooks" / "pre-commit"
    result, before, after = _apply(workspace, HOOK_COPY)
    assert not hook.exists(), (result["status"], result["response_text"])
    assert result["ok"] is False, (result["status"], result["response_text"])
    assert after == before


# Python's `str.splitlines` also breaks a line at these characters, and the engines do not: VOOL read a
# shorter name than the one they create.
@pytest.mark.parametrize("mark", ["\x0b", "\x0c", "\x1c", "\x85", " "])
@pytest.mark.parametrize("engines", ["git_present", "without_git"])
def test_a_name_with_a_character_python_splits_lines_at_creates_no_unchecked_file(
    workspace: Path, request: pytest.FixtureRequest, engines: str, mark: str
) -> None:
    if engines == "without_git":
        request.getfixturevalue("without_git")
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n"
        f"--- /dev/null\n+++ b/new{mark}data.txt\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    )
    _assert_lands_only_where_checked(*_apply(workspace, patch))


# GNU patch also reads a section indented by a space (it says "Patch is indented 1 space"); VOOL did not.
def test_gnu_patch_creates_no_file_from_an_indented_section(workspace: Path, without_git: None) -> None:
    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-one\n+two\n\n"
        " --- /dev/null\n +++ b/new.sh\n @@ -0,0 +1,2 @@\n +x\n +y\n"
    )
    _assert_lands_only_where_checked(*_apply(workspace, patch))


# A link the patch itself creates would carry a later section's writes wherever it points.
def test_a_patch_that_creates_a_symbolic_link_is_refused_with_nothing_written(workspace: Path) -> None:
    patch = (
        "diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+data\n"
        "\\ No newline at end of file\n"
        "diff --git a/l/y.txt b/l/y.txt\nnew file mode 100644\n--- /dev/null\n+++ b/l/y.txt\n@@ -0,0 +1 @@\n+x\n"
    )
    result, before, after = _apply(workspace, patch)
    assert result["ok"] is False and result["status"] == "invalid_arguments", (result["status"], result["response_text"])
    assert after == before and not (workspace / "l").is_symlink()


# GNU patch also reads an indented `diff --git` section, and with POSIXLY_CORRECT set it takes the name an
# `Index:` line gives; VOOL reads neither.
def test_gnu_patch_creates_no_file_from_an_indented_git_header(workspace: Path, without_git: None) -> None:
    patch = (
        "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n\n"
        " diff --git a/data/evil b/data/evil\n new file mode 100644\n\ntrailing text\n"
    )
    _assert_lands_only_where_checked(*_apply(workspace, patch))


def test_gnu_patch_does_not_take_an_index_line_name(
    workspace: Path, without_git: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSIXLY_CORRECT", "1")
    patch = "Index: data/x.txt\n--- nope.txt\n+++ nope.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n\ntrailing text\n"
    _assert_lands_only_where_checked(*_apply(workspace, patch))


# A removed `-- comment` line reads `--- comment` in the hunk; it is content, not a file header.
def test_removing_a_line_that_starts_with_two_dashes_still_applies_with_git(workspace: Path) -> None:
    (workspace / "q.sql").write_bytes(b"a\n-- drop table t;\nc\n")
    result, before, after = _apply(workspace, "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n a\n--- drop table t;\n c\n")
    assert result["ok"] is True and result["details"]["engine"] == "git_apply", (result["status"], result["response_text"])
    assert after["q.sql"] == b"a\nc\n"


# GNU patch also takes a `---` line with no `+++` after it, or one a blank line parts from its `+++`, as a
# header, and strips it with the `-p` it was given.
LONE_HEADER_CASES = {
    "lone_old_name": (
        "--- c/data/x.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+P\n c\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+n\n"
    ),
    "pair_parted_by_a_blank_line": (
        "--- c/data/x.txt\n\n+++ c/data/x.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+P\n c\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+n\n"
    ),
    "lone_prefixed_name_beside_plain_headers": (
        "--- a/data/x.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+P\n c\n--- /dev/null\n+++ new.txt\n@@ -0,0 +1 @@\n+n\n"
    ),
}


@pytest.mark.parametrize("patch", LONE_HEADER_CASES.values(), ids=LONE_HEADER_CASES.keys())
def test_gnu_patch_writes_nothing_from_a_header_without_its_partner(workspace: Path, patch: str) -> None:
    _assert_lands_only_where_checked(*_apply(workspace, patch))


# GNU patch also takes a `*** name` line (a context diff's header) as a file name, even told `--unified`.
CONTEXT_NAME_CASES = {
    "changes_a_file": (
        "--- a/new.txt\n*** b/data/x.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+P\n c\n--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1 @@\n+x\n"
    ),
    "creates_a_file": "--- /dev/null\n*** b/data/evil\n@@ -0,0 +1 @@\n+x\n--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1 @@\n+x\n",
}


@pytest.mark.parametrize("patch", CONTEXT_NAME_CASES.values(), ids=CONTEXT_NAME_CASES.keys())
def test_gnu_patch_takes_no_name_from_a_context_diff_line(workspace: Path, patch: str) -> None:
    _assert_lands_only_where_checked(*_apply(workspace, patch))


# GNU patch keeps a `.orig` copy beside a file it placed at an offset and writes a `.rej` file beside one
# it could not patch; VOOL removes the ones its run made, and only those.
@pytest.mark.parametrize("taken", ["ok.txt.orig", "ok.txt.rej"])
def test_gnu_patch_is_not_started_over_a_file_already_at_its_backup_name(
    workspace: Path, without_git: None, taken: str
) -> None:
    (workspace / taken).write_bytes(b"the user's own file\n")
    result, before, after = _apply(workspace, "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n")
    assert result["ok"] is False, (result["status"], result["response_text"])
    assert taken in result["response_text"]
    assert after == before


def test_gnu_patch_leaves_no_reject_file_beside_a_file_it_could_not_patch(workspace: Path, without_git: None) -> None:
    result, before, after = _apply(workspace, "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-nowhere\n+B\n")
    assert result["ok"] is False, (result["status"], result["response_text"])
    assert after == before


@pytest.mark.parametrize("variable", ["SIMPLE_BACKUP_SUFFIX", "VERSION_CONTROL", "PATCH_VERSION_CONTROL", "PATCH_GET"])
def test_gnu_patch_is_not_started_when_a_variable_changes_what_it_reads_or_leaves(
    workspace: Path, without_git: None, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, {"SIMPLE_BACKUP_SUFFIX": ".keep", "PATCH_GET": "1"}.get(variable, "numbered"))
    result, before, after = _apply(workspace, "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n")
    assert result["ok"] is False, (result["status"], result["response_text"])
    assert variable in result["response_text"]
    assert after == before


@pytest.mark.parametrize("suffix", [".orig", ".rej"])
def test_gnu_patch_is_not_started_when_the_patch_itself_names_a_backup_name(
    workspace: Path, without_git: None, suffix: str
) -> None:
    # VOOL removes the `.orig`/`.rej` files a run made; one the patch creates is not GNU patch's to lose.
    patch = (
        "--- a/ok.txt\n+++ b/ok.txt\n@@ -5,1 +5,1 @@\n-b\n+B\n"
        f"--- /dev/null\n+++ b/ok.txt{suffix}\n@@ -0,0 +1 @@\n+mine\n"
    )
    result, before, after = _apply(workspace, patch)
    assert result["ok"] is False, (result["status"], result["response_text"])
    assert f"ok.txt{suffix}" in result["response_text"]
    assert after == before
