"""Windows: a project folder may not be a system folder, a Program Files folder, ProgramData, the
profile itself or the folder holding every profile (K1).

``_SYSTEM_ROOTS`` in core/project_store.py only names POSIX folders, so on Windows a project could be
bound to ``C:\\Windows\\System32`` or ``C:\\Program Files``. The Windows folders are taken from the
shell's known folders (SHGetKnownFolderPath via core/windows_known_folders.py), with the environment
as a second source, and are compared with Windows rules (case and separators). The shell is faked
here so the Windows decision runs on any platform.
"""
from __future__ import annotations

import pytest

from core import project_store as ps
from core import windows_known_folders, windows_path_policy

_SHELL = {
    "Profile": r"C:\Users\me",
    "Windows": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFilesX86": r"C:\Program Files (x86)",
    "ProgramData": r"C:\ProgramData",
    "LocalAppData": r"C:\Users\me\AppData\Local",
    "RoamingAppData": r"C:\Users\me\AppData\Roaming",
    "Documents": r"C:\Users\me\Documents",
}


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    shell = dict(_SHELL)
    monkeypatch.setattr(windows_known_folders, "_shell_known_folder", lambda name: shell.get(name))
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False)
    # The POSIX home checks must not be what refuses these spellings.
    monkeypatch.setenv("HOME", str(tmp_path / "posix-home"))
    for name in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                 "ProgramData", "USERPROFILE", "SystemDrive"):
        monkeypatch.delenv(name, raising=False)
    return shell


@pytest.mark.parametrize("folder", [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"c:\windows\system32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files\Some App",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\ProgramData\Microsoft",
    r"C:\Users\me",
    r"C:\USERS\ME",
    r"C:\Users",
    r"C:\Users\me\AppData",
    r"C:\Users\me\AppData\Local",
    r"C:\Users\me\AppData\Roaming",
])
def test_windows_system_folders_are_refused(windows, folder) -> None:
    refusal = ps._too_broad_root(folder)
    assert refusal, folder
    assert refusal[0].islower()


@pytest.mark.parametrize("folder", [
    r"C:\Users\me\Documents\repo",
    r"C:\Users\me\code",
    r"D:\work\project",
    r"C:\src\vool",
])
def test_windows_normal_folders_still_pass(windows, folder) -> None:
    assert ps._too_broad_root(folder) == ""


def test_windows_folder_is_refused_from_the_environment_when_the_shell_cannot_answer(windows, monkeypatch) -> None:
    windows.clear()
    monkeypatch.setenv("SystemRoot", r"D:\WINNT")
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    monkeypatch.setenv("ProgramData", r"D:\AppState")
    monkeypatch.setenv("USERPROFILE", r"D:\Profiles\me")
    for folder in (r"D:\WINNT", r"D:\WINNT\System32", r"D:\Apps", r"D:\AppState", r"D:\Profiles\me", r"D:\Profiles"):
        assert ps._too_broad_root(folder), folder
    assert ps._too_broad_root(r"D:\Profiles\me\repo") == ""


def test_windows_folder_is_refused_even_with_no_shell_and_no_environment(windows) -> None:
    """Unknown states fail closed: with nothing to ask, the default system folder is still refused."""
    windows.clear()
    assert ps._too_broad_root(r"C:\Windows\System32")
    assert ps._too_broad_root(r"C:\Program Files")
    assert ps._too_broad_root(r"C:\ProgramData")


def test_off_windows_the_windows_spellings_add_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(tmp_path / "posix-home"))
    folder = tmp_path / "Program Files"
    folder.mkdir()
    assert ps._too_broad_root(str(folder)) == ""
