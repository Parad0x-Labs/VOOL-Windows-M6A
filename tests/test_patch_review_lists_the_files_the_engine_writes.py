"""A patch's review lists exactly the files VOOL's engine writes for it, read from the same parse.

The review of a code-task patch records one destination per file the patch names
(`task_runtime._destination_paths`), and the approved patch is then applied by the protected writer,
which renders with VOOL's own engine (`_apply_unified_diff_python`). The review read the names with the
header scanner (`_extract_patch_paths`), which lists every `---`/`+++` line, so a removed `-- x` line
(patch line `--- x`) put a file `x` in the review that the engine never writes. The review and the
apply step now come from one parse (`patch_review_paths`). The header scanner still checks every name
before an external engine runs off Windows; that protection is unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.code_assistant import task_runtime
from core.execution import workspace_tools

SQL = b"create table t (id int);\n-- drop table t;\nselect 1;\n"
LUA = b"a\n-- old\n" + b"".join(f"m{n}\n".encode() for n in range(1, 9)) + b"z\n"

PATCHES = {
    "removed_dash_line": (
        {"q.sql": SQL},
        "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n--- drop table t;\n select 1;\n",
    ),
    "removal_and_addition_read_like_a_header": (
        {"n.lua": LUA},
        "--- a/n.lua\n+++ b/n.lua\n@@ -1,2 +1,2 @@\n a\n--- old\n+++ new\n@@ -10,2 +10,2 @@\n m8\n-z\n+Z\n",
    ),
    "two_files_git_format": (
        {"q.sql": SQL, "n.lua": b"-- lua comment\nreturn 1\n"},
        "diff --git a/q.sql b/q.sql\nindex 1111111..2222222 100644\n"
        "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
        "diff --git a/n.lua b/n.lua\nindex 3333333..4444444 100644\n"
        "--- a/n.lua\n+++ b/n.lua\n@@ -1,2 +1,1 @@\n--- lua comment\n return 1\n",
    ),
}


def _engine_writes(root: Path, files: dict[str, bytes], patch: str) -> list[str]:
    for name, data in files.items():
        (root / name).write_bytes(data)
    renderable, unsupported = workspace_tools._protected_patch_text(patch)
    assert not unsupported
    written: list[str] = []
    workspace_tools._apply_unified_diff_python(
        renderable, workspace_root=root, sink=lambda path, _text: written.append(path.relative_to(root).as_posix())
    )
    return written


@pytest.mark.parametrize("files, patch", PATCHES.values(), ids=PATCHES.keys())
def test_the_review_lists_exactly_the_files_the_engine_writes(tmp_path: Path, files, patch) -> None:
    root = tmp_path.resolve()

    reviewed = task_runtime._destination_paths("workspace.apply_unified_diff", {"patch": patch})

    assert reviewed == _engine_writes(root, files, patch)


@pytest.mark.parametrize("files, patch", PATCHES.values(), ids=PATCHES.keys())
def test_an_approved_patch_reviewed_on_those_files_applies(tmp_path: Path, files, patch) -> None:
    # The protected writer refuses a file the review did not record; recorded as the review lists them,
    # the approved patch applies and changes exactly those files.
    root = tmp_path.resolve()
    for name, data in files.items():
        (root / name).write_bytes(data)
    destinations = workspace_tools._destinations_as_found(
        patch, task_runtime._destination_paths("workspace.apply_unified_diff", {"patch": patch}), workspace_root=root
    )
    assert isinstance(destinations, list), destinations

    result = workspace_tools.apply_unified_diff_workspace(
        {"patch": patch}, workspace_root=root, session_id="review-paths", reviewed_destinations=destinations
    )

    assert result["ok"] is True, (result["status"], result["response_text"])
    assert sorted(result["details"]["changed_paths"]) == sorted(files)


@pytest.mark.parametrize("files, patch", PATCHES.values(), ids=PATCHES.keys())
def test_on_windows_the_patch_tool_names_only_the_files_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files, patch
) -> None:
    root = tmp_path.resolve() / "marked"
    root.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(root))
    for name, data in files.items():
        (root / name).write_bytes(data)

    result = workspace_tools.apply_unified_diff_workspace({"patch": patch}, workspace_root=root, session_id="review-paths")

    assert result["ok"] is True, (result["status"], result["response_text"])
    assert sorted(result["details"]["paths"]) == sorted(result["details"]["changed_paths"]) == sorted(files)


# A hunk whose lines do not match its `@@` counts is refused as `unsupported_patch` with nothing written:
# in such a hunk a removed `-- x` line cannot be told from the next file's header.
COUNT_MISMATCHES = {
    "claims_more_lines_than_it_holds": (
        "--- a/q.sql\n+++ b/q.sql\n@@ -1,5 +1,4 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
    ),
    "holds_more_lines_than_it_claims": (
        "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
    ),
    "claims_more_and_is_followed_by_another_file": (
        "--- a/q.sql\n+++ b/q.sql\n@@ -1,5 +1,4 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"
        "--- a/n.lua\n+++ b/n.lua\n@@ -1 +1 @@\n-a\n+b\n"
    ),
}


@pytest.mark.parametrize("patch", COUNT_MISMATCHES.values(), ids=COUNT_MISMATCHES.keys())
def test_on_windows_a_hunk_whose_counts_do_not_match_is_refused_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: str
) -> None:
    root = tmp_path.resolve() / "marked"
    root.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(root))
    (root / "q.sql").write_bytes(SQL)
    (root / "n.lua").write_bytes(b"a\n")

    result = workspace_tools.apply_unified_diff_workspace({"patch": patch}, workspace_root=root, session_id="counts")

    assert result["ok"] is False and result["status"] == "unsupported_patch", (result["status"], result["response_text"])
    assert "Nothing was written" in result["response_text"]
    assert (root / "q.sql").read_bytes() == SQL and (root / "n.lua").read_bytes() == b"a\n"
