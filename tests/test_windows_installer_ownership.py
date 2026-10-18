"""The Windows installer stops only VOOL's own processes before replacing files.

It used to force-stop whatever process listened on 127.0.0.1:11435, with no ownership check, so
installing or upgrading VOOL could kill an unrelated program that happened to use that port.
VOOL's own API server is still stopped by the ownership-checked filter: a python/pythonw that
lives under the install directory or runs one of VOOL's scripts.
"""
from __future__ import annotations

from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"


def _stop_command() -> str:
    text = ISS.read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if "Stop-Process" in line)


def test_the_installer_never_stops_a_process_just_because_it_owns_the_api_port() -> None:
    command = _stop_command()
    assert "Get-NetTCPConnection" not in command
    assert "OwningProcess" not in command


def test_the_installer_still_stops_vools_own_python_processes() -> None:
    command = _stop_command()
    assert "Programs\\VOOL" in command
    # Path-boundary match: a sibling such as ...\Programs\VOOL-dev is not the install directory.
    assert "$_.ExecutablePath -like ($r + ''\\*'')" in command
    assert "vool_api_server.py" in command
    assert "vool_window.py" in command
