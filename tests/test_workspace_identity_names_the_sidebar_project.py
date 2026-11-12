"""The chat names a chat's project the way the sidebar does, and calls a folder a folder.

Found on Steve (consistency pass, row 5): a chat that is in no project sits under "General" in the
sidebar, but asking "which project am I in?" answered "That is the project `Desktop`". Desktop is
the default working folder, not a project, so the same chat had two different names for where it
lives. The sidebar's projects are `core.project_store` entries; the turn carries the chat's
project id. The answer must name that project, or "General" when there is none, and must never
promote the working folder's name to a project name.
"""
from __future__ import annotations

import pytest

from core import project_store, runtime_paths
from core.runtime_execution_tools import execute_runtime_tool


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    yield
    runtime_paths.configure_runtime_home(None)


def _identity(workspace, **context) -> str:
    source_context = {"workspace": str(workspace), "surface": "api", **context}
    result = execute_runtime_tool("workspace.identity", {}, source_context=source_context)
    assert result.ok, result
    return str(result.response_text)


def test_a_chat_in_no_project_is_in_general_and_the_folder_is_not_called_a_project(tmp_path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    text = _identity(desktop)

    assert "General" in text
    assert "the project `Desktop`" not in text
    assert "`Desktop`" in text, "the working folder is still named, as a folder"


def test_an_unknown_project_id_reads_as_general_not_as_the_folder(tmp_path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    text = _identity(desktop, project_id="proj_missing")

    assert "General" in text
    assert "the project `Desktop`" not in text


def test_a_chat_in_a_project_names_the_sidebar_project(tmp_path) -> None:
    folder = tmp_path / "calc-repo"
    folder.mkdir()
    ok, project = project_store.create_project("Calculator", str(folder))
    assert ok, project

    text = _identity(folder, _trusted_project_id=project["id"])

    assert "the project `Calculator`" in text
    assert "General" not in text
    assert "`calc-repo`" in text


def test_renaming_the_project_is_not_hidden_by_a_remembered_answer(tmp_path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    ok, project = project_store.create_project("Before", str(folder))
    assert ok
    assert "`Before`" in _identity(folder, _trusted_project_id=project["id"])

    ok, renamed = project_store.create_project("After", str(folder))
    assert ok and renamed["id"] == project["id"]

    assert "`After`" in _identity(folder, _trusted_project_id=project["id"])
