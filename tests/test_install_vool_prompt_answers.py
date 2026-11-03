"""install_vool.bat must use the answers typed at its prompts.

cmd expands %VAR% when it parses a whole ( ) block, before any line in it runs. A `set /p`
answer read inside a block is therefore invisible to %VAR% later in that block: the empty check
sees the old value, so the default overwrites the answer, and the validation checks the old
value. Inside the block the answer has to be read as !VAR! (delayed expansion).
"""
from __future__ import annotations

import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "install_vool.bat"
PROMPT = re.compile(r'^(\s*)set /p "(\w+)=', re.IGNORECASE)


def _lines() -> list[str]:
    return INSTALLER.read_text(encoding="utf-8").splitlines()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _prompts() -> list[tuple[int, str, list[str]]]:
    """Each interactive prompt, with the rest of the ( ) block it sits in."""
    lines = _lines()
    found = []
    for number, line in enumerate(lines):
        match = PROMPT.match(line)
        if not match or not match.group(1):
            continue
        rest = []
        for later in lines[number + 1:]:
            if later.strip() and _indent(later) < len(match.group(1)):
                break
            rest.append(later)
        found.append((number + 1, match.group(2), rest))
    return found


def test_the_installer_has_delayed_expansion_on() -> None:
    assert any(line.strip().lower() == "setlocal enabledelayedexpansion" for line in _lines()[:5])


def test_the_installer_prompts_are_found() -> None:
    assert {name for _, name, _ in _prompts()} == {"VOOL_HOME", "AGENT_NAME", "INSTALL_PROFILE_OVERRIDE"}


def test_prompt_answers_are_read_with_delayed_expansion_inside_their_block() -> None:
    stale = [
        f"install_vool.bat:{number} {name}: {later.strip()}"
        for number, name, rest in _prompts()
        for later in rest
        if f"%{name}%".lower() in later.lower()
    ]
    assert stale == []
    for _, name, rest in _prompts():
        assert any(f"!{name}!" in later for later in rest), name
