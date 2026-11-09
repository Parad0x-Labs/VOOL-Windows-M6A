"""In 0.7.1 a hunk whose lines do not match its `@@` counts is refused on Windows only.

On Windows VOOL's own engine is the only one, and it refuses such a hunk as `unsupported_patch` with
nothing written. Off Windows the earlier behaviour stays for 0.7.1: the engine reads the hunk as far as
it goes (and `git apply --recount` recounts it), so a patch that applied before still applies, including
an approved code-task patch, which the protected writer renders with the same engine. The gate is
switched through `windows_write_gate._IS_WINDOWS`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.code_assistant import task_runtime
from core.execution import workspace_tools

SQL = b"create table t (id int);\n-- drop table t;\nselect 1;\n"
AFTER = b"create table t (id int);\nselect 1;\n"
# The hunk claims five old lines and four new ones; it holds three and two.
MISCOUNTED = "--- a/q.sql\n+++ b/q.sql\n@@ -1,5 +1,4 @@\n create table t (id int);\n--- drop table t;\n select 1;\n"


def test_on_windows_a_miscounted_hunk_is_refused_as_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve() / "marked"
    root.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(root))
    (root / "q.sql").write_bytes(SQL)

    result = workspace_tools.apply_unified_diff_workspace({"patch": MISCOUNTED}, workspace_root=root, session_id="counts")

    assert result["ok"] is False and result["status"] == "unsupported_patch", (result["status"], result["response_text"])
    assert (root / "q.sql").read_bytes() == SQL


def test_off_windows_a_miscounted_hunk_still_applies_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    engine_root = tmp_path.resolve() / "engine"
    engine_root.mkdir()
    (engine_root / "q.sql").write_bytes(SQL)

    workspace_tools._apply_unified_diff_python(MISCOUNTED, workspace_root=engine_root)

    assert (engine_root / "q.sql").read_bytes() == AFTER

    # An approved code-task patch goes through the protected writer, which renders with the same engine.
    reviewed_root = tmp_path.resolve() / "reviewed"
    reviewed_root.mkdir()
    (reviewed_root / "q.sql").write_bytes(SQL)
    paths = task_runtime._destination_paths("workspace.apply_unified_diff", {"patch": MISCOUNTED})
    assert paths == ["q.sql"]
    destinations = workspace_tools._destinations_as_found(MISCOUNTED, paths, workspace_root=reviewed_root)

    result = workspace_tools.apply_unified_diff_workspace(
        {"patch": MISCOUNTED}, workspace_root=reviewed_root, session_id="counts", reviewed_destinations=destinations
    )

    assert result["ok"] is True, (result["status"], result["response_text"])
    assert (reviewed_root / "q.sql").read_bytes() == AFTER
