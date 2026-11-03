"""vool_background.cmd must hand its project path to PowerShell without pasting it into code.

cmd expands %SCRIPT_ROOT% before PowerShell parses the -Command text. A path with an apostrophe
(C:\\Users\\O'Brien\\...) then ends the '...' string early and the command fails to parse. For the
self-update lock check that failure exits 1, which the script reads as "stale lock cleared", so
the watchdog restarts the server in the middle of a self-update. PowerShell reads the path from
the environment instead ($env:SCRIPT_ROOT; cmd's `set` exports it to the child).
"""
from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "vool_background.cmd"


def _powershell_commands() -> list[str]:
    return [line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if line.lstrip().lower().startswith("powershell ")]


def test_no_powershell_command_pastes_a_cmd_variable_into_its_code() -> None:
    pasted = [line.strip() for line in _powershell_commands() if "%" in line.split("-Command", 1)[-1]]
    assert pasted == []


def test_the_update_lock_check_reads_the_project_path_from_the_environment() -> None:
    lock_checks = [line for line in _powershell_commands() if ".vool_updating.lock" in line]
    assert len(lock_checks) == 1
    assert "$env:SCRIPT_ROOT" in lock_checks[0]


def test_the_direct_start_reads_the_project_path_from_the_environment() -> None:
    starts = [line for line in _powershell_commands() if "Start_VOOL.bat" in line]
    assert len(starts) == 1
    assert "$env:SCRIPT_ROOT" in starts[0]
