"""Open_Chat.bat and Open_Web0.bat start Python (the --rearm call) in UTF-8 mode, like every other launcher.

Without PYTHONUTF8 Python on Windows reads and writes text as cp1252: an install path or VOOL home with
non-ASCII letters is read wrongly by installer.vool_stop --rearm. The other launchers set it right after
``setlocal`` (Start_VOOL.bat, Stop_VOOL.bat, installer/bundle/vool-launch.cmd); .bat files cannot run here,
so the line and its place before the first Python call are pinned.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["Open_Chat.bat", "Open_Web0.bat"])
def test_open_launcher_sets_utf8_mode_before_the_rearm(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    assert 'set "PYTHONUTF8=1"' in lines, f"{name} must set PYTHONUTF8=1 at the top level"
    assert text.index('set "PYTHONUTF8=1"') < text.index("-m installer.vool_stop --rearm")
    assert text.index('set "PYTHONUTF8=1"') < text.index("python.exe")
