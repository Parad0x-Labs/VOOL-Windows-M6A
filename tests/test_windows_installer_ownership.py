"""The Windows installer stops only VOOL's own processes before replacing files.

It used to force-stop whatever process listened on 127.0.0.1:11435, with no ownership check, so
installing or upgrading VOOL could kill an unrelated program that happened to use that port.
VOOL's own API server is still stopped by the ownership-checked filter: the bundle's own
python.exe or pythonw.exe, or a python/pythonw running one of VOOL's scripts under the install
directory.
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
    # The install root is the actual {app} directory, not a hard-coded Programs\VOOL path.
    assert "InstallDir := ExpandConstant('{app}');" in ISS.read_text(encoding="utf-8")
    assert "$r = ''' + InstallDir + '''" in command
    # Literal, case-insensitive path match: a sibling such as ...\VOOL-dev, or a root that looks
    # like a wildcard pattern, is not the install directory.
    assert "$_.ExecutablePath -like" not in command
    assert "$rootPrefix = $r.TrimEnd([char[]]@(92,47)) + [char]92" in command
    # By its exe only the bundle's own interpreter counts, at its exact path: a user's own Python
    # inside the install folder is not VOOL's.
    assert "$python = $rootPrefix + ''python\\python.exe''; $pythonw = $rootPrefix + ''python\\pythonw.exe''" in command
    assert (
        "[string]::Equals([string]$_.ExecutablePath, $python, [System.StringComparison]::OrdinalIgnoreCase) "
        "-or [string]::Equals([string]$_.ExecutablePath, $pythonw, [System.StringComparison]::OrdinalIgnoreCase)"
        in command
    )
    assert "StartsWith($rootPrefix" not in command
    # VOOL's own scripts are matched only under the install root, regex-escaped.
    assert "[regex]::Escape($rootPrefix)" in command
    assert "vool_api_server\\.py" in command
    assert "vool_window\\.py" in command
