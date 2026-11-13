"""VOOL's own patch engine reads a hunk's body by the `@@` line counts, so a content line is never a header.

A removed line that starts with `-- ` (an SQL or Lua comment, a text line in a YAML or Markdown file)
reads `--- ...` in the patch. `_apply_unified_diff_python` (core/execution/workspace_tools.py) took any
`--- ` line as the next file's header, so the patch failed as "Malformed unified diff: missing `+++`
header" and nothing was written. On Windows this engine is the only one (no git apply, no GNU patch), so
such a removal could not be applied at all. Off Windows `git apply` usually applied it first.

The engine now counts each hunk's old and new lines from its `@@ -a,b +c,d @@` header: while the hunk
still has old lines to read, a `--- ` line is a removal. A diff that is really malformed is still refused,
with nothing written.

The Windows rows switch the write gate on (`windows_write_gate._IS_WINDOWS`, with the workspace flagged as
a disposable folder) and go through `apply_unified_diff_workspace`, which then uses this engine only
(`protected_python`). The engine rows call the engine directly, as the Linux fallback does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.execution import workspace_tools

SQL = b"create table t (id int);\n-- drop table t;\nselect 1;\n"
SQL_PATCH = (
    "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
)

SQL_TWO_HUNKS = b"".join(
    [b"-- header comment\n", b"select 1;\n"] + [f"line {n}\n".encode() for n in range(3, 11)]
    + [b"-- footer comment\n", b"select 2;\n"]
)
SQL_TWO_HUNKS_PATCH = (
    "--- a/q.sql\n+++ b/q.sql\n"
    "@@ -1,3 +1,2 @@\n--- header comment\n select 1;\n line 3\n"
    "@@ -10,3 +9,2 @@\n line 10\n--- footer comment\n select 2;\n"
)
SQL_TWO_HUNKS_AFTER = b"".join(
    [b"select 1;\n"] + [f"line {n}\n".encode() for n in range(3, 11)] + [b"select 2;\n"]
)

# The first hunk ends by removing `-- old` and adding `++ new`: the patch lines read `--- old` and
# `+++ new`, a header pair, right before the next hunk's `@@`.
PAIR_LIKE_A_HEADER = b"a\n-- old\n" + b"".join(f"m{n}\n".encode() for n in range(1, 9)) + b"z\n"
PAIR_LIKE_A_HEADER_PATCH = (
    "--- a/n.lua\n+++ b/n.lua\n"
    "@@ -1,2 +1,2 @@\n a\n--- old\n+++ new\n"
    "@@ -10,2 +10,2 @@\n m8\n-z\n+Z\n"
)
PAIR_LIKE_A_HEADER_AFTER = b"a\n++ new\n" + b"".join(f"m{n}\n".encode() for n in range(1, 9)) + b"Z\n"

YAML = b"---\nname: a\n---\nname: b\n"
YAML_PATCH = "--- a/c.yaml\n+++ b/c.yaml\n@@ -1,4 +1,3 @@\n ---\n name: a\n----\n name: b\n"
YAML_DASH_TEXT = b"notes: |\n  keep\n-- generated, do not edit\nname: a\n"
YAML_DASH_TEXT_PATCH = (
    "--- a/c.yaml\n+++ b/c.yaml\n@@ -1,4 +1,3 @@\n notes: |\n   keep\n--- generated, do not edit\n name: a\n"
)

MULTI_FILE_PATCH = (
    "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
    "--- a/c.yaml\n+++ b/c.yaml\n@@ -1,4 +1,3 @@\n ---\n name: a\n----\n name: b\n"
)

GIT_MULTI_FILE_PATCH = (
    "diff --git a/q.sql b/q.sql\nindex 1111111..2222222 100644\n"
    "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
    "diff --git a/n.lua b/n.lua\nindex 3333333..4444444 100644\n"
    "--- a/n.lua\n+++ b/n.lua\n@@ -1,2 +1,1 @@\n--- lua comment\n return 1\n"
)

APPLIES = {
    "sql_comment_removed": ({"q.sql": SQL}, SQL_PATCH, {"q.sql": b"create table t (id int);\nselect 1;\n"}),
    "sql_two_hunks": ({"q.sql": SQL_TWO_HUNKS}, SQL_TWO_HUNKS_PATCH, {"q.sql": SQL_TWO_HUNKS_AFTER}),
    "removal_and_addition_read_like_a_header": (
        {"n.lua": PAIR_LIKE_A_HEADER}, PAIR_LIKE_A_HEADER_PATCH, {"n.lua": PAIR_LIKE_A_HEADER_AFTER}
    ),
    "yaml_document_marker_removed": ({"c.yaml": YAML}, YAML_PATCH, {"c.yaml": b"---\nname: a\nname: b\n"}),
    "yaml_text_line_with_two_dashes_removed": (
        {"c.yaml": YAML_DASH_TEXT}, YAML_DASH_TEXT_PATCH, {"c.yaml": b"notes: |\n  keep\nname: a\n"}
    ),
    "two_files": (
        {"q.sql": SQL, "c.yaml": YAML}, MULTI_FILE_PATCH,
        {"q.sql": b"create table t (id int);\nselect 1;\n", "c.yaml": b"---\nname: a\nname: b\n"},
    ),
    "two_files_git_format": (
        {"q.sql": SQL, "n.lua": b"-- lua comment\nreturn 1\n"}, GIT_MULTI_FILE_PATCH,
        {"q.sql": b"create table t (id int);\nselect 1;\n", "n.lua": b"return 1\n"},
    ),
}

REFUSED = {
    # A `---` header with no `+++` after it.
    "header_without_its_partner": ({"q.sql": SQL}, "--- a/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n"
                                   "--- drop table t;\n select 1;\n"),
    # The hunk has read all its lines; the `--- ` line after it is a header, and no `+++` follows.
    "dash_line_after_a_complete_hunk": ({"q.sql": SQL}, "--- a/q.sql\n+++ b/q.sql\n@@ -1,1 +1,1 @@\n"
                                        "-create table t (id int);\n+create table u (id int);\n--- drop table t;\n"),
    "bad_hunk_header": ({"q.sql": SQL}, "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1 @\n create table t (id int);\n"),
    "no_hunks": ({"q.sql": SQL}, "--- a/q.sql\n+++ b/q.sql\n"),
    "content_that_is_not_a_hunk_line": ({"q.sql": SQL}, "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n"
                                        " create table t (id int);\n--- drop table t;\n select 1;\nnot a hunk line\n"),
}


def _seed(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        (root / name).write_bytes(data)


def _contents(root: Path, names) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in names}


@pytest.fixture
def windows_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path.resolve() / "marked"
    workspace.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    return workspace


# The engine itself reads no `diff --git`/`index` lines: the Windows patch tool drops them first
# (`_protected_patch_text`), and off Windows `git apply` takes a git-format patch before this engine runs.
ENGINE_APPLIES = {name: case for name, case in APPLIES.items() if name != "two_files_git_format"}


@pytest.mark.parametrize("files, patch, expected", ENGINE_APPLIES.values(), ids=ENGINE_APPLIES.keys())
def test_the_engine_applies_a_patch_whose_content_lines_read_like_headers(
    tmp_path: Path, files, patch, expected
) -> None:
    _seed(tmp_path, files)

    workspace_tools._apply_unified_diff_python(patch, workspace_root=tmp_path)

    assert _contents(tmp_path, expected) == expected


@pytest.mark.parametrize("files, patch, expected", APPLIES.values(), ids=APPLIES.keys())
def test_on_windows_the_patch_tool_applies_it(windows_workspace: Path, files, patch, expected) -> None:
    _seed(windows_workspace, files)

    result = workspace_tools.apply_unified_diff_workspace(
        {"patch": patch}, workspace_root=windows_workspace, session_id="dash-lines"
    )

    assert result["ok"] is True, (result["status"], result["response_text"])
    assert result["details"]["engine"] == "protected_python", result["details"]
    assert _contents(windows_workspace, expected) == expected
    assert sorted(result["details"]["changed_paths"]) == sorted(expected), result["details"]


@pytest.mark.parametrize("files, patch", REFUSED.values(), ids=REFUSED.keys())
def test_the_engine_still_refuses_a_malformed_diff(tmp_path: Path, files, patch) -> None:
    _seed(tmp_path, files)

    # Called without a sink the engine writes each file as it renders it (its callers put files back on
    # a failure), so only the refusal is asserted here; the Windows rows below assert nothing is written.
    with pytest.raises(ValueError, match=r"(?i)malformed unified diff|unsupported unified diff line"):
        workspace_tools._apply_unified_diff_python(patch, workspace_root=tmp_path)


@pytest.mark.parametrize("files, patch", REFUSED.values(), ids=REFUSED.keys())
def test_on_windows_a_malformed_diff_is_refused_with_nothing_written(windows_workspace: Path, files, patch) -> None:
    _seed(windows_workspace, files)

    result = workspace_tools.apply_unified_diff_workspace(
        {"patch": patch}, workspace_root=windows_workspace, session_id="dash-lines"
    )

    assert result["ok"] is False
    assert result["status"] == "invalid_patch_syntax", (result["status"], result["response_text"])
    assert "Nothing was written" in result["response_text"]
    assert _contents(windows_workspace, files) == files
    assert sorted(p.name for p in windows_workspace.iterdir()) == sorted(files)


def test_on_windows_a_file_with_crlf_line_endings_is_still_refused(windows_workspace: Path) -> None:
    # Not changed here: the Windows patch tool cannot keep CRLF bytes exactly, so it refuses the file.
    (windows_workspace / "q.sql").write_bytes(SQL.replace(b"\n", b"\r\n"))

    result = workspace_tools.apply_unified_diff_workspace(
        {"patch": SQL_PATCH}, workspace_root=windows_workspace, session_id="dash-lines"
    )

    assert result["ok"] is False and result["status"] == "unsupported_patch", (result["status"], result["response_text"])
    assert (windows_workspace / "q.sql").read_bytes() == SQL.replace(b"\n", b"\r\n")
