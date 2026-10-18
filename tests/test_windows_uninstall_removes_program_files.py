"""Uninstalling VOOL on Windows removes its program folder and never the user's data.

The bundle ships no bytecode, so the installed app writes __pycache__ under {app}\\app as it
imports modules. The uninstaller only deletes what it installed, so install smoke run 39098297286
uninstalled with exit 0 and left 115 package folders and the install folder itself behind. Every
[UninstallDelete] entry must stay inside the program folder: the user's models, wallet, keys and
memory in {localappdata}\\VOOL are preserved.
"""
from __future__ import annotations

import re
from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"


def _uninstall_delete_entries() -> list[str]:
    text = ISS.read_text(encoding="utf-8")
    match = re.search(r"^\[UninstallDelete\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, "vool.iss has no [UninstallDelete] section"
    return [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith(";")]


def test_the_uninstaller_removes_the_runtime_written_program_tree() -> None:
    assert 'Type: filesandordirs; Name: "{app}\\app"' in _uninstall_delete_entries()


def test_every_uninstall_delete_stays_inside_the_program_folder() -> None:
    for entry in _uninstall_delete_entries():
        name = re.search(r'Name:\s*"([^"]+)"', entry)
        assert name, entry
        assert name.group(1).startswith("{app}\\"), f"uninstall would delete outside the program folder: {entry}"
