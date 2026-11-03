"""install_vool.bat must not paste the runtime folder into Python source.

cmd expands %VOOL_HOME% before Python parses the -c text. A runtime folder typed at the prompt (or
given with /NULLAHOME=) ending in a backslash breaks r'...'; one holding an apostrophe ends the
string and the rest of the answer runs as Python. The -c programs read it from the environment
instead (cmd's `set` exports every variable to the child). The other values pasted there are VOOL's
own: model tags it printed itself and an install profile already checked against a fixed list.
"""
from __future__ import annotations

import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "install_vool.bat"
PYTHON_C = re.compile(r'python\.exe"\s+-c\s+"(?P<code>[^"]*)"', re.IGNORECASE)


def _python_programs() -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(INSTALLER.read_text(encoding="utf-8").splitlines(), start=1):
        found.extend((number, match.group("code")) for match in PYTHON_C.finditer(line))
    return found


def test_the_runtime_folder_reaches_python_programs() -> None:
    assert sum("runtime_home=" in code for _, code in _python_programs()) == 3


def test_no_python_program_has_the_runtime_folder_pasted_into_its_source() -> None:
    pasted = [f"install_vool.bat:{number}" for number, code in _python_programs() if "%VOOL_HOME%" in code.upper()]
    assert pasted == []
