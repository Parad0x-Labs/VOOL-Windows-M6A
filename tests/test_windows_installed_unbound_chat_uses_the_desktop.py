"""In the installed Windows app, a chat with no project works in the user's Desktop.

The owner's 2026-09-18 decision put the packaged app's unbound chats on the Desktop, but it keyed
on VOOL_PROJECT_ROOT, which only the macOS app and the source-checkout launchers set. The installed
Windows app sets VOOL_BUNDLE_ROOT instead, so its unbound chats got the API's working directory,
{app}\\app: the program folder, which runtime state protection refuses to write and uninstall
deletes. The owner chose the Desktop for Windows too (2026-10-20).

The Windows write gate is unchanged: a write there is still refused until the Desktop is listed in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES. Notes stay in the VOOL home either way.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from core.web.api import runtime as api_runtime
from core.web.api.runtime import default_workspace_root


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in ("VOOL_PROJECT_ROOT", "VOOL_WORKSPACE_ROOT", "VOOL_BUNDLE_ROOT", gate.ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))


def _real_desktop() -> Path:
    if sys.platform == "win32":
        return api_runtime._windows_desktop()
    return Path.home() / "Desktop"


def test_the_installed_app_gives_an_unbound_chat_the_desktop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_BUNDLE_ROOT", str(tmp_path / "Programs" / "VOOL"))

    assert default_workspace_root() == str(_real_desktop())


def test_a_chosen_workspace_still_wins_in_the_installed_app(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_BUNDLE_ROOT", str(tmp_path / "Programs" / "VOOL"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(tmp_path / "chosen"))

    assert default_workspace_root() == str((tmp_path / "chosen").resolve())


def test_a_blank_bundle_root_is_not_the_installed_app(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_BUNDLE_ROOT", "   ")
    monkeypatch.chdir(tmp_path)

    assert default_workspace_root() == str(tmp_path.resolve())


def test_the_installed_app_does_not_tick_the_setup_folder_step(tmp_path, monkeypatch) -> None:
    from core import setup_progress

    monkeypatch.setenv("VOOL_BUNDLE_ROOT", str(tmp_path / "Programs" / "VOOL"))
    monkeypatch.setattr("core.project_store.list_projects", lambda: [])

    assert setup_progress.folder_done() is False


def test_the_write_gate_still_refuses_the_desktop_until_it_is_flagged(tmp_path, monkeypatch) -> None:
    """The real dispatch, with the workspace an unbound chat gets in the installed app."""
    from core.runtime_execution_tools import _dispatch_runtime_tool

    desktop = tmp_path / "user" / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setenv("VOOL_BUNDLE_ROOT", str(tmp_path / "Programs" / "VOOL"))
    monkeypatch.setattr(api_runtime, "_windows_desktop", lambda: desktop)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    workspace = default_workspace_root()
    assert Path(workspace) == desktop

    refused = _dispatch_runtime_tool("workspace.write_file", {"path": "hello.txt", "content": "hi"},
                                     source_context={"workspace": workspace, "workspace_root": workspace})
    assert refused is not None and refused.ok is False and refused.status == gate.STATUS
    assert not (desktop / "hello.txt").exists()

    monkeypatch.setenv(gate.ENV_VAR, str(desktop))
    allowed = _dispatch_runtime_tool("workspace.ensure_directory", {"path": "made"},
                                     source_context={"workspace": workspace, "workspace_root": workspace})
    assert allowed is not None and allowed.status != gate.STATUS


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows shell's known Desktop folder")
def test_on_windows_it_is_the_shells_desktop_not_a_guessed_path() -> None:
    shell = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert shell.returncode == 0, shell.stderr
    assert api_runtime._windows_desktop() == Path(shell.stdout.strip())
