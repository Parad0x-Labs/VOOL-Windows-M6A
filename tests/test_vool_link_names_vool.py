"""A browser opening a vool:// link asks to open VOOL, not Python.

Bug hunt 0.7.1 (379e57f7), B12: the vool:// registration took its icon from pythonw.exe and gave
no name, so the OpenRouter sign-in prompt read "Open Python?". The link must show VOOL's installed
icon and carry the friendly name VOOL. The handler command itself is unchanged.

Inno Setup cannot run here, so this reads vool.iss statically, like the other installer tests.
"""
from __future__ import annotations

import re
from pathlib import Path

ISS = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"


def _registry() -> list[str]:
    text = ISS.read_text(encoding="utf-8")
    match = re.search(r"^\[Registry\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, "vool.iss has no [Registry] section"
    return [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith(";")]


def _entries(subkey: str) -> list[str]:
    needle = f'Subkey: "{subkey}";'
    return [line for line in _registry() if needle in line]


def test_the_link_icon_is_vools_installed_icon() -> None:
    icons = _entries(r"Software\Classes\vool\DefaultIcon")
    assert len(icons) == 1, icons
    assert r'ValueData: "{app}\vool.ico,0"' in icons[0], icons[0]
    assert "pythonw" not in icons[0].lower()


def test_the_link_is_named_vool() -> None:
    names = [line for line in _entries(r"Software\Classes\vool\shell\open") if 'ValueName: "FriendlyAppName"' in line]
    assert len(names) == 1, names
    assert 'ValueData: "VOOL"' in names[0]
    assert "HKCU" in names[0], "per-user install: never HKLM"


def test_the_handler_command_is_unchanged() -> None:
    commands = _entries(r"Software\Classes\vool\shell\open\command")
    assert len(commands) == 1
    assert r'""{app}\python\pythonw.exe"" -X utf8 ""{app}\vool_protocol_handler.py"" ""%1""' in commands[0]


def test_the_vool_icon_is_installed_for_the_link_to_use() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert re.search(r'Source:\s*"\{#SourcePath\}\\\.\.\\assets\\vool\.ico";\s*DestDir:\s*"\{app\}"', text)
