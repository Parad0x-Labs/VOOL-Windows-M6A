"""Start_VOOL.bat's stale-server stop must be scoped to this checkout.

Before a fresh start the launcher stops a stale apps.vool_api_server that holds its log and
database. Matching on the command line alone also stopped a server another VOOL checkout on the
same machine was running. On Windows a venv's Scripts\\python(w).exe is a launcher that runs the
base interpreter as a child, so the server process itself runs the base python.exe: the stop
accepts a process when it, or its parent (the venv launcher), runs from this checkout's
.venv\\Scripts folder. The path comes from $env:SCRIPT_ROOT, never pasted into PowerShell code.
"""
from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "Start_VOOL.bat"


def _stop_line() -> str:
    lines = [line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
             if line.lstrip().lower().startswith("powershell ") and "Stop-Process" in line]
    assert len(lines) == 1
    return lines[0]


def test_the_stale_server_stop_is_scoped_to_this_checkouts_venv() -> None:
    line = _stop_line()
    assert "$env:SCRIPT_ROOT" in line
    assert ".venv\\Scripts\\" in line
    assert "ExecutablePath" in line


def test_the_scope_also_accepts_the_venv_launchers_child() -> None:
    assert "ParentProcessId" in _stop_line()


def test_no_cmd_variable_is_pasted_into_the_stop_code() -> None:
    assert "%" not in _stop_line().split("-Command", 1)[1]
