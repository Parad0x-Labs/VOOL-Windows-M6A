"""Batch launchers started through cmd /c must stay quoted whatever their path holds.

Popen([comspec, "/c", path]) lets list2cmdline quote the path, and cmd then strips those quotes
unless the line has exactly two quotes and no & < > ( ) @ ^ | between them. A path such as
C:\\Users\\A&B\\VOOL or C:\\Program Files (x86)\\VOOL then splits or runs the text after "&".
`/s /c ""path""` makes cmd strip only the outer pair, so the path keeps its own quotes.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from installer import self_update
from installer.bundle import vool_protocol_handler
from tests._windows_system32 import trust_system32


def _cmd_line_after_s_c(command: str) -> str:
    """What cmd /s /c runs: the text after /c with its first and last quote removed."""
    tail = command.split(" /c ", 1)[1]
    assert tail.startswith('"') and tail.endswith('"'), command
    return tail[1:-1]


def test_self_update_restart_keeps_the_launcher_path_quoted(tmp_path, monkeypatch) -> None:
    root = tmp_path / "A&B (x86)"
    root.mkdir()
    (root / "Start_VOOL.bat").write_text("@echo off\n", encoding="utf-8")
    launched: list[object] = []
    trust_system32(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    self_update._default_start_server(root)
    assert len(launched) == 1 and isinstance(launched[0], str)
    assert " /s /c " in launched[0]
    assert _cmd_line_after_s_c(launched[0]) == f'"{root / "Start_VOOL.bat"}"'


def test_protocol_handler_start_keeps_the_launcher_path_quoted(tmp_path, monkeypatch) -> None:
    root = tmp_path / "A&B (x86)"
    root.mkdir()
    (root / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    launched: list[object] = []
    trust_system32(monkeypatch)
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    vool_protocol_handler._start_bundle(root)
    assert len(launched) == 1 and isinstance(launched[0], str)
    assert launched[0].startswith('"C:\\Windows\\System32\\cmd.exe" /s /c ')
    assert _cmd_line_after_s_c(launched[0]) == f'"{root / "VOOL.cmd"}"'


@pytest.mark.parametrize("comspec", [None, "C:\\Users\\me\\evil.exe", "cmd.exe"])
def test_protocol_handler_runs_cmd_from_system32_whatever_comspec_says(tmp_path, monkeypatch, comspec) -> None:
    """ComSpec is a user variable: the handler runs %SystemRoot%\\System32\\cmd.exe by absolute path."""
    root = tmp_path / "VOOL"
    root.mkdir()
    (root / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    launched: list[object] = []
    trust_system32(monkeypatch)
    if comspec is None:
        monkeypatch.delenv("ComSpec", raising=False)
        monkeypatch.delenv("COMSPEC", raising=False)
    else:
        monkeypatch.setenv("ComSpec", comspec)
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    vool_protocol_handler._start_bundle(root)
    assert len(launched) == 1 and launched[0].startswith('"C:\\Windows\\System32\\cmd.exe" /s /c ')


@pytest.mark.parametrize("system_root", ["", "Windows", "\\\\server\\share\\Windows", "C:", "C:Windows"])
def test_protocol_handler_starts_nothing_when_system32_cannot_be_trusted(tmp_path, monkeypatch, system_root) -> None:
    root = tmp_path / "VOOL"
    root.mkdir()
    (root / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", system_root)
    monkeypatch.setenv("ComSpec", "C:\\Windows\\System32\\cmd.exe")
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may start"))
    with pytest.raises(OSError):
        vool_protocol_handler._start_bundle(root)
