"""What cmd /s /c cannot keep literal inside quotes is refused, and an argument stays one argument.

Quotes stop cmd from acting on & ( ) ^ |, but not on %NAME%, which it expands inside quotes with
no escape on a /c line, and not on a line break, which ends the command. Those are refused the
way a double quote already is. A quoted argument ending in backslashes needs them doubled: a
program the .cmd hands %* to (node, python) reads \\" as an escaped quote and would merge it
with the next argument, which is how list2cmdline quoted it before.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from installer import self_update, start_windows_detached
from installer.bundle import vool_protocol_handler
from tests._windows_system32 import trust_system32


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(start_windows_detached, "_is_windows_platform", lambda: True)
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")


@pytest.mark.parametrize("bad", ["100%OS%", "a\rb", "a\nb"])
def test_wrap_command_refuses_what_cmd_would_expand_or_split(on_windows, bad) -> None:
    with pytest.raises(ValueError):
        start_windows_detached._wrap_command_for_windows(["C:\\vool\\openclaw.cmd", bad])
    with pytest.raises(ValueError):
        start_windows_detached._wrap_command_for_windows([f"C:\\{bad}\\openclaw.cmd", "run"])


def test_wrap_command_doubles_trailing_backslashes_before_the_closing_quote(on_windows) -> None:
    wrapped = start_windows_detached._wrap_command_for_windows(
        ["C:\\vool\\openclaw.cmd", "--dir", "C:\\data\\", "C:\\a\\b\\\\", "x"]
    )
    assert wrapped == (
        '"C:\\Windows\\System32\\cmd.exe" /s /c '
        '""C:\\vool\\openclaw.cmd" "--dir" "C:\\data\\\\" "C:\\a\\b\\\\\\\\" "x""'
    )


_CMD_UNSAFE = ["100%OS%", "a\rb", "a\nb", 'a"b']


class _NamedPath:
    """A path by name only. NTFS cannot hold a quote or a line break in a name, so these cases
    cannot be made on disk on Windows; the launchers only join, test and print the path."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __truediv__(self, name: str) -> _NamedPath:
        return _NamedPath(f"{self._text}\\{name}")

    def is_file(self) -> bool:
        return True

    def exists(self) -> bool:
        return True

    def __str__(self) -> str:
        return self._text


@pytest.mark.parametrize("bad", _CMD_UNSAFE)
def test_protocol_handler_refuses_a_launcher_path_cmd_would_expand(monkeypatch, bad) -> None:
    launched: list[object] = []
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    with pytest.raises(ValueError):
        vool_protocol_handler._start_bundle(_NamedPath(f"C:\\root{bad}"))
    assert launched == []


@pytest.mark.parametrize("bad", _CMD_UNSAFE)
def test_self_update_refuses_a_launcher_path_cmd_would_expand(monkeypatch, bad) -> None:
    launched: list[object] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    self_update._default_start_server(_NamedPath(f"C:\\root{bad}"))
    assert launched == []


def test_a_plain_launcher_path_still_starts(monkeypatch) -> None:
    # The refusal is not a blanket one: the same fake with an ordinary path is launched.
    launched: list[object] = []
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    vool_protocol_handler._start_bundle(_NamedPath("C:\\Program Files (x86)\\A&B"))
    trust_system32(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    self_update._default_start_server(_NamedPath("C:\\Program Files (x86)\\A&B"))
    assert len(launched) == 2
