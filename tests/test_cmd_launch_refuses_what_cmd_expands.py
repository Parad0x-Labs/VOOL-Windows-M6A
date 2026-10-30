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


@pytest.mark.parametrize("bad", _CMD_UNSAFE)
def test_protocol_handler_refuses_a_launcher_path_cmd_would_expand(tmp_path, monkeypatch, bad) -> None:
    root = tmp_path / f"root{bad}"
    root.mkdir()
    (root / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    launched: list[object] = []
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    with pytest.raises(ValueError):
        vool_protocol_handler._start_bundle(root)
    assert launched == []


@pytest.mark.parametrize("bad", _CMD_UNSAFE)
def test_self_update_refuses_a_launcher_path_cmd_would_expand(tmp_path, monkeypatch, bad) -> None:
    root = tmp_path / f"root{bad}"
    root.mkdir()
    (root / "Start_VOOL.bat").write_text("@echo off\n", encoding="utf-8")
    launched: list[object] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    self_update._default_start_server(root)
    assert launched == []
