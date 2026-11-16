"""Off Windows the workspace-identity answer is exactly Mac's.

Jesse's standing rule (16 Nov 2026): fixes are Windows-only by default, and Mac and Linux behave as
Mac ships them. The "This chat is in General" answer (tests/test_workspace_identity_names_the_sidebar_project.py)
fixed a Windows consistency finding, so off Windows the answer stays Mac's: the workspace path, then
"That is the project `<folder>`" with its top-level counts.
"""
from __future__ import annotations

import pytest

from core import runtime_paths, windows_write_gate
from core.runtime_execution_tools import execute_runtime_tool


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def test_off_windows_the_answer_is_macs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "notes.txt").write_text("x", encoding="utf-8")

    result = execute_runtime_tool(
        "workspace.identity", {}, source_context={"workspace": str(desktop), "surface": "api"}
    )

    assert result.ok, result
    assert result.response_text == (
        f"The workspace is set to `{desktop}`. "
        "That is the project `Desktop`, holding 1 file and 0 folders at the top level."
    )


def test_on_windows_the_answer_names_the_sidebar_project(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    result = execute_runtime_tool(
        "workspace.identity", {}, source_context={"workspace": str(desktop), "surface": "api"}
    )

    assert "This chat is in General, not in a project." in result.response_text
    assert "the project `Desktop`" not in result.response_text
