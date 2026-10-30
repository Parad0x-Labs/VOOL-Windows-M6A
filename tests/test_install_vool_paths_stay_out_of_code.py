"""install_vool.bat keeps folder paths and the agent name out of code cmd or PowerShell parses.

A path pasted into a single-quoted PowerShell string ends the string at an apostrophe, so
C:\\Users\\O'Brien breaks the Liquefy patch and the Ollama download (%TEMP% is under the profile).
PowerShell reads the folder from $env: instead. The closing summary echoes the agent name, which
the user types, and the project folder: with %VAR% cmd parses a & in them as a second command;
with !VAR! the value is expanded after cmd has parsed the line.
"""
from __future__ import annotations

import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "install_vool.bat"
_PATH_VARS = ("LIQUEFY_DIR", "TEMP")


def _lines() -> list[str]:
    return INSTALLER.read_text(encoding="utf-8").splitlines()


def test_no_folder_path_is_pasted_into_a_single_quoted_powershell_string() -> None:
    pasted = [
        (number, line.strip())
        for number, line in enumerate(_lines(), start=1)
        if "powershell" in line.lower()
        and any(re.search(rf"'%{name}%", line, re.IGNORECASE) for name in _PATH_VARS)
    ]
    assert pasted == []


def test_the_liquefy_patch_and_ollama_download_read_their_folders_from_the_environment() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "Join-Path $env:LIQUEFY_DIR 'pyproject.toml'" in text
    assert "-OutFile (Join-Path $env:TEMP 'OllamaSetup.exe')" in text


def test_the_closing_summary_echoes_typed_values_with_delayed_expansion() -> None:
    echoed = [
        line.strip()
        for line in _lines()
        if line.lstrip().lower().startswith("echo ") and re.search(r"%(AGENT_NAME|PROJECT_ROOT)%", line)
    ]
    assert echoed == []
