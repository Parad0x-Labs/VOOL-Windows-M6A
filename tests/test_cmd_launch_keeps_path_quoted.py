"""Batch launchers started through cmd /c must stay quoted whatever their path holds.

Popen([comspec, "/c", path]) lets list2cmdline quote the path, and cmd then strips those quotes
unless the line has exactly two quotes and no & < > ( ) @ ^ | between them. A path such as
C:\\Users\\A&B\\VOOL or C:\\Program Files (x86)\\VOOL then splits or runs the text after "&".
`/s /c ""path""` makes cmd strip only the outer pair, so the path keeps its own quotes.
"""
from __future__ import annotations

import subprocess
import sys

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
    monkeypatch.setenv("ComSpec", "C:\\Windows\\System32\\cmd.exe")
    monkeypatch.setattr(vool_protocol_handler.subprocess, "Popen", lambda cmd, **kwargs: launched.append(cmd))
    vool_protocol_handler._start_bundle(root)
    assert len(launched) == 1 and isinstance(launched[0], str)
    assert launched[0].startswith('"C:\\Windows\\System32\\cmd.exe" /s /c ')
    assert _cmd_line_after_s_c(launched[0]) == f'"{root / "VOOL.cmd"}"'
