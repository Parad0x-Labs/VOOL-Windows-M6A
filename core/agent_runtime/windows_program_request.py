"""On Windows, answer "run <program>" for a program VOOL never runs there, before any model is called.

Clean-VM capability test C12/C7 (0.7.1-beta): "run calc.exe", "run cmd /c dir", "run powershell
Get-Process" and "run node -e ..." each went to the chat model, which spent minutes deciding on a
`sandbox.run_command` call; that call then asked for approval, and only after approval did the job
runner's phase-1 gate refuse the program. The 0.7.1 limits say VOOL runs only Python and pytest in
its sandbox on Windows, so the answer was known before the model was asked.

This module only READS the request: it finds the command line a whole-message "run/execute/launch/
start <command>" names. Whether that command is refused is not decided here. The front door asks the
same pre-decision check a `sandbox.run_command` tool call is asked
(`core.runtime_execution_tools.windows_write_refusal_before_decision`), whose program verdict is the
job runner's own phase-1 gate. A Python or pytest command passes that check and the turn goes on as
before.

A request is read as a command only when its program is unmistakably one: the command is quoted or
in backticks, the program has a Windows program suffix (`calc.exe`, `setup.msi`, `build.cmd`), or it
is one of a closed set of program names people type as commands. "run the tests", "start a new
project" and "launch the builder" are not commands, so they are left to the lanes that own them.
"""

from __future__ import annotations

import re

_REQUEST_RE = re.compile(
    r"^\s*(?:(?:please|pls|can\s+you|could\s+you|would\s+you|vool)[\s,]+)*"
    r"(?:run|execute|exec|launch|start)\s+"
    r"(?:(?:the|this)\s+)?(?:(?:command|program|app|application|executable)\s+)?"
    r"(?P<command>.+?)\s*(?:,?\s*(?:please|for\s+me|now))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
#: Suffixes that make a word a Windows program file.
_PROGRAM_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".scr")
#: Program names that are typed as commands and are not ordinary English after "run". Recognising a
#: name here only lets the request be ASKED about; Python and pytest are listed so that they reach
#: the gate and pass it, never so that anything is refused by name.
_COMMAND_NAMES = frozenset(
    {
        "cmd", "powershell", "pwsh", "wsl", "bash", "sh", "zsh",
        "node", "nodejs", "npm", "npx", "yarn", "pnpm", "deno", "bun",
        "ruff", "pip", "pip3", "pipx", "uv", "git", "cargo", "rustc", "go", "java", "javac", "dotnet",
        "calc", "notepad", "mspaint", "explorer", "regedit", "taskkill", "tasklist", "msiexec",
        "winget", "choco", "curl", "wget", "ls", "dir",
        "python", "python3", "py", "pytest",
    }
)
_QUOTES = {"`": "`", '"': '"', "'": "'"}
#: "lint this with ruff", "format the project using ruff": the tool is named, so the request is a
#: command for that tool (`ruff check .` / `ruff format .`). Only tools named in `_COMMAND_NAMES`.
_TOOL_REQUEST_RE = re.compile(
    r"^\s*(?:(?:please|pls|can\s+you|could\s+you|would\s+you|vool)[\s,]+)*"
    r"(?P<verb>lint|check|format)\b[^\n]{0,60}?\b(?:with|using|via|through)\s+(?P<tool>[\w.-]+)"
    r"\s*(?:,?\s*(?:please|for\s+me|now))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_TOOL_COMMANDS = {"ruff": {"lint": "ruff check .", "check": "ruff check .", "format": "ruff format ."}}


def requested_command(text: str) -> str:
    """The command line a whole-message run request names, or "" when the message is not one.

    One line only: a request that carries a script or several lines is not a single command."""
    raw = str(text or "")
    if not raw.strip() or "\n" in raw.strip():
        return ""
    tool = _TOOL_REQUEST_RE.match(raw)
    if tool:
        commands = _TOOL_COMMANDS.get(tool.group("tool").lower())
        if commands:
            return commands[tool.group("verb").lower()]
    match = _REQUEST_RE.match(raw)
    if not match:
        return ""
    command = match.group("command").strip()
    quoted = False
    if len(command) >= 2 and command[0] in _QUOTES and command[-1] == _QUOTES[command[0]]:
        command = command[1:-1].strip()
        quoted = True
    elif command.startswith("```") or command.startswith("`"):
        return ""  # an unbalanced or fenced code span is not a command we can read
    if not command:
        return ""
    program = command.split()[0].strip("\"'")
    name = program.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if quoted or name.endswith(_PROGRAM_SUFFIXES) or name in _COMMAND_NAMES:
        return command
    return ""


def windows_program_request_refusal(text: str, *, source_context: dict | None = None) -> str:
    """On Windows, the refusal a run request gets because its program never runs there; "" when the
    message is not a run request, its command would pass the pre-decision checks, or off Windows."""
    from sandbox.job_runner import _is_windows_platform

    if not _is_windows_platform():
        return ""
    command = requested_command(text)
    if not command:
        return ""
    from core.runtime_execution_tools import windows_write_refusal_before_decision

    refusal = windows_write_refusal_before_decision(
        "sandbox.run_command", {"command": command}, source_context=source_context
    )
    return str(refusal.response_text or "") if refusal is not None else ""


__all__ = ["requested_command", "windows_program_request_refusal"]
