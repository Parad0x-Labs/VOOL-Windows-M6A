"""On Windows the browser lane finds the Edge installed in Program Files, and only Edge.

Off Windows it looks on PATH (Linux) or in /Applications (macOS). On Windows it looked on PATH for
Linux command names, so a machine with Edge (every Windows install) answered `engine_missing`. It
now looks only at the fixed install locations under the Program Files folders the shell reports;
PATH, a per-user install and a Program Files folder on a share are never used. The profile guard
also reads a Windows spelling of a real Chrome or Edge profile.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import windows_known_folders
from core.vool_browser import engine


def _install(base: Path, relative: str) -> Path:
    exe = base.joinpath(*relative.split("\\"))
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    exe.chmod(0o755)
    return exe


@pytest.fixture()
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    monkeypatch.delenv("VOOL_BROWSER_ENGINE", raising=False)
    monkeypatch.setattr(engine.sys, "platform", "win32")
    folders = {"x64": tmp_path / "Program Files", "x86": tmp_path / "Program Files (x86)"}
    for folder in folders.values():
        folder.mkdir()
    monkeypatch.setattr(windows_known_folders, "program_files_folders", lambda: [str(f) for f in folders.values()])
    # A browser on PATH under a Linux name is never what Windows uses.
    on_path = tmp_path / "bin"
    _install(on_path, "chromium")
    monkeypatch.setenv("PATH", str(on_path) + os.pathsep + os.environ.get("PATH", ""))
    return folders


def test_edge_in_program_files_x86_is_found(windows: dict[str, Path]) -> None:
    exe = _install(windows["x86"], "Microsoft\\Edge\\Application\\msedge.exe")
    assert engine.find_engine_binary() == str(exe)


def test_edge_in_program_files_is_found(windows: dict[str, Path]) -> None:
    exe = _install(windows["x64"], "Microsoft\\Edge\\Application\\msedge.exe")
    assert engine.find_engine_binary() == str(exe)


def test_edge_wins_over_chrome_in_another_program_files_folder(windows: dict[str, Path]) -> None:
    # As on the hosted runner: Chrome in Program Files, Edge in Program Files (x86).
    _install(windows["x64"], "Google\\Chrome\\Application\\chrome.exe")
    exe = _install(windows["x86"], "Microsoft\\Edge\\Application\\msedge.exe")
    assert engine.find_engine_binary() == str(exe)


def test_chrome_alone_is_not_used_on_windows(windows: dict[str, Path]) -> None:
    _install(windows["x64"], "Google\\Chrome\\Application\\chrome.exe")
    assert engine.find_engine_binary() is None


def test_with_nothing_installed_the_answer_is_none_not_path(windows: dict[str, Path]) -> None:
    assert engine.find_engine_binary() is None


def test_a_share_reported_as_program_files_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    reported = {"ProgramFiles": "\\\\host\\share\\Program Files", "ProgramFilesX86": "C:\\Program Files (x86)"}
    monkeypatch.setattr(windows_known_folders, "_shell_known_folder", lambda name: reported.get(name))
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False)
    assert windows_known_folders.program_files_folders() == ["C:\\Program Files (x86)"]


def test_a_network_drive_reported_as_program_files_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    reported = {"ProgramFiles": "Z:\\Program Files", "ProgramFilesX86": "C:\\Program Files (x86)"}
    monkeypatch.setattr(windows_known_folders, "_shell_known_folder", lambda name: reported.get(name))
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive.upper() == "Z:")
    assert windows_known_folders.program_files_folders() == ["C:\\Program Files (x86)"]


def test_off_windows_no_program_files_folder_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", False)
    assert windows_known_folders.program_files_folders() == []


@pytest.mark.parametrize("profile", [
    "C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data",
    "C:\\Users\\me\\AppData\\Local\\Microsoft\\Edge\\User Data\\Default",
    "c:/users/me/appdata/local/chromium/user data",
    "C:\\Users\\me\\AppData\\Local\\BraveSoftware\\Brave-Browser\\User Data",
])
def test_a_windows_spelling_of_a_real_browser_profile_is_refused(profile: str) -> None:
    assert engine.refuse_real_profile_target(profile) is not None
