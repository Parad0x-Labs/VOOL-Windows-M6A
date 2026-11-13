"""Windows: a project folder may not be a system folder, a Program Files folder, ProgramData, the
profile itself or the folder holding every profile (K1).

``_SYSTEM_ROOTS`` in core/project_store.py only names POSIX folders, so on Windows a project could be
bound to ``C:\\Windows\\System32`` or ``C:\\Program Files``. The Windows folders are taken from the
shell's known folders (SHGetKnownFolderPath via core/windows_known_folders.py), with the environment
as a second source, and are compared with Windows rules (case and separators). The shell is faked
here so the Windows decision runs on any platform.
"""
from __future__ import annotations

import os

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


@pytest.mark.parametrize("profile", [r"\\server\profiles\me", r"Z:\profiles\me"])
def test_windows_profile_on_a_share_is_never_resolved(windows, monkeypatch, profile) -> None:
    """A profile (or home) on a share or a network drive is compared by its spelling only: resolving it
    would already be the SMB connection that leaks the user's NTLM hash."""
    windows.pop("Profile")
    monkeypatch.setenv("USERPROFILE", profile)
    monkeypatch.setenv("HOME", profile)  # what expanduser("~") answers on this host
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive.upper() == "Z:")
    looked_up: list[str] = []
    real = os.path.realpath

    def realpath(path, *args, **kwargs):
        looked_up.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    assert ps._too_broad_root(r"C:\Users\me\Documents\repo") == ""
    assert ps._too_broad_root(profile)
    reached = [p for p in looked_up if p.replace("/", "\\").lower().startswith((r"\\server", "z:"))]
    assert reached == []


@pytest.mark.parametrize("typed", [r"Z:\proj", r"z:\work\repo", "Z:/proj"])
def test_windows_a_typed_folder_on_a_network_drive_is_refused_before_any_lookup(windows, monkeypatch, typed) -> None:
    """A drive letter Windows does not call local (a mapped network drive is a share under a letter) is
    refused by its spelling and drive type, before realpath: resolving it would already open the SMB
    connection (Security Architect, 0.7.2 merge review)."""
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive.upper() == "Z:")
    looked_up: list[str] = []
    real = os.path.realpath

    def realpath(path, *args, **kwargs):
        looked_up.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    ok, message = ps.create_project("p", typed)
    assert ok is False
    assert "network drive" in message and message[0].islower(), message
    assert [p for p in looked_up if p.replace("/", "\\").lower().startswith("z:")] == []


def test_windows_a_drive_windows_cannot_type_is_refused_too(windows, monkeypatch) -> None:
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: None)
    ok, message = ps.create_project("p", r"Q:\proj")
    assert ok is False and "network drive" in message, message


def test_windows_a_local_drive_is_not_refused_as_a_network_drive(windows) -> None:
    ok, message = ps.create_project("p", r"C:\Users\me\Documents\x")
    assert "network drive" not in str(message)
