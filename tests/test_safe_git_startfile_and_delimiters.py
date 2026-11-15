"""os.startfile's arguments and git bounded by { } , = are checked for git (E2-seams-01#1, #2).

Uses only core.safe_git and pytest's monkeypatch. No audit hook is installed (one cannot be removed):
_spawn_audit is called directly with _REFUSE_GIT and _GUARD_ARMED patched, and monkeypatch puts both back.
"""
from __future__ import annotations

import pytest

from core import safe_git


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)
    monkeypatch.setattr(safe_git, "_GUARD_ARMED", True)


@pytest.mark.parametrize("path, arguments", [
    ("cmd.exe", "/c git status"),
    ("C:\\Windows\\System32\\cmd.exe", "/c git,status"),
    ("powershell.exe", "-c &{git status}"),
    ("pwsh", "-Command git"),
])
def test_startfile_of_a_wrapper_whose_arguments_run_git_is_refused(armed, path, arguments) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.startfile/2", (path, "open", arguments, None, 1))


def test_a_startfile_event_that_cannot_be_read_is_refused(armed) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.startfile/2", ("cmd.exe",))


def test_startfile_of_a_document_or_a_wrapper_without_git_still_starts(armed) -> None:
    safe_git._spawn_audit("os.startfile", ("notes.txt", "open"))
    safe_git._spawn_audit("os.startfile/2", ("notes.txt", "open", None, None, 1))
    safe_git._spawn_audit("os.startfile/2", ("cmd.exe", "open", "/c echo .gitignore", None, 1))


def test_startfile_of_git_itself_is_still_refused(armed) -> None:
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.startfile", ("C:\\Program Files\\Git\\cmd\\git.exe", "open"))


@pytest.mark.parametrize("command", [
    ["powershell", "-c", "&{git status}"],
    ["powershell", "-c", "&{git}"],
    "cmd /c git,status",
    "cmd /c set p=git&&%p% status",
])
def test_git_bounded_by_braces_commas_or_equals_is_found(command) -> None:
    assert safe_git.is_git_command(command)


@pytest.mark.parametrize("command", [
    ["cmd", "/c", "echo .gitignore"],
    ["powershell", "-c", "$git = 1"],
    ["sh", "-c", "legit digit gitter"],
    ["cmd", "/c", "type my-git.txt"],
])
def test_names_that_only_contain_git_are_not_git(command) -> None:
    assert not safe_git.is_git_command(command)


@pytest.mark.parametrize("command", ["cd C:\\ws\\repo && git status", "git status", "set p=git&& %p% status"])
def test_os_system_text_is_read_as_the_shells(armed, command) -> None:
    """system() runs its text through cmd and no later start event follows (L1 review #2)."""
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.system", (command,))


def test_os_system_text_without_git_still_runs(armed) -> None:
    safe_git._spawn_audit("os.system", ("",))  # the console-mode call core.honesty_receipt makes
    safe_git._spawn_audit("os.system", ("echo hi",))


@pytest.mark.parametrize("operation", ["git_gui", "git_shell", "runas", "explore"])
def test_a_startfile_verb_other_than_open_is_refused(armed, operation) -> None:
    """A registered shell verb can start git with no git in the path (L1 review #1)."""
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.startfile", ("C:\\ws\\repo", operation))
    with pytest.raises(safe_git.GitRefusedOnWindows):
        safe_git._spawn_audit("os.startfile/2", ("C:\\ws\\repo", operation, None, None, 1))


def test_control_read_as_bare_text_a_cd_then_git_is_not_found(armed) -> None:
    """Why the os.system rows need the shell reading: as bare text the program is `cd`."""
    assert not safe_git.is_git_command("cd C:\\ws\\repo && git status")


@pytest.mark.parametrize("command", [["cmd", "/c", "echo tests/git/x"], ["sh", "-c", "type C:\\src\\git\\notes.txt"]])
def test_a_folder_named_git_on_a_path_is_not_git(command) -> None:
    """The bounded name search must not read a path folder as the program (test_windows_runs_no_git)."""
    assert safe_git.is_git_command(command) is False


@pytest.mark.parametrize("command", [["cmd", "/c", "git. status"], ["cmd", "/c", "C:\\Git\\cmd\\git.exe status"]])
def test_git_with_a_trailing_dot_or_behind_its_folder_is_still_git(command) -> None:
    """Windows drops a trailing dot, so git. runs git; the Git folder does not hide the git.exe after it."""
    assert safe_git.is_git_command(command) is True
