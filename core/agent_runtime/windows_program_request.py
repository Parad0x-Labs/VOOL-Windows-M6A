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

A message that is itself a command line for an installer or tool ("pip install requests", "py -m pip
install x", "npm install") or an install with a named installer ("install requests with pip") is read
the same way, with no "run" before it (clean-VM round 1: a bare "pip install requests" got a model's
offer to help instead of the refusal).
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
_LEAD = r"^\s*(?:(?:please|pls|can\s+you|could\s+you|would\s+you|vool)[\s,]+)*"
_TAIL = r"\s*(?:,?\s*(?:please|for\s+me|now))?\s*[.!?]?\s*$"
#: A whole message that IS a command line, with no "run" before it: "pip install requests",
#: "py -m pip install x", "npm install", "ruff check .". Only programs whose next word is one of their
#: own subcommands or a flag, so a sentence that starts with the word ("pip is slow", "python is
#: fun") is not read as one. A Python command line must start with a flag (`python -m ...`); a script
#: run ("python app.py") passes the gate anyway, and "python ..." prose is left alone.
_BARE_COMMAND_RE = re.compile(
    _LEAD
    + r"(?P<command>"
    r"(?:pip|pip3|pipx|pip3\.\d+)(?:\.exe)?\s+(?:install|uninstall|download|wheel|freeze|list|show|check|"
    r"config|cache|inspect|index|search|hash|debug)\b[^\n]*?"
    r"|(?:py|python|python3)(?:\.exe)?\s+-[^\n]*?"
    r"|(?:npm|pnpm)(?:\.cmd)?\s+(?:install|i|ci|run|test|start|init|exec|uninstall|update|audit|publish|"
    r"build|add)\b[^\n]*?"
    r"|yarn\s+(?:install|add|run|build|test|start)\b[^\n]*?"
    r"|npx(?:\.cmd)?\s+\S[^\n]*?"
    r"|ruff(?:\.exe)?\s+(?:check|format|rule|clean|server)\b[^\n]*?"
    r")"
    + _TAIL,
    re.IGNORECASE,
)
_INSTALLERS = r"pip|pip3|pipx|npm|pnpm|yarn|uv|conda"
#: "install requests with pip", "add lodash using npm", "use pip to install requests": an install
#: with a named installer is that installer's install command.
_INSTALL_WITH_RE = re.compile(
    _LEAD
    + r"(?:(?:install|add)\s+(?P<packages>[^\n]+?)\s+(?:with|using|via|through)\s+(?P<tool>"
    + _INSTALLERS
    + r")|use\s+(?P<tool2>"
    + _INSTALLERS
    + r")\s+to\s+(?:install|add)\s+(?P<packages2>[^\n]+?))"
    + _TAIL,
    re.IGNORECASE,
)


def requested_command(text: str) -> str:
    """The command line a whole-message run request names (or the message itself, when it is a bare
    installer or tool command line), or "" when the message is not one.

    One line only: a request that carries a script or several lines is not a single command."""
    raw = str(text or "")
    if not raw.strip() or "\n" in raw.strip():
        return ""
    tool = _TOOL_REQUEST_RE.match(raw)
    if tool:
        commands = _TOOL_COMMANDS.get(tool.group("tool").lower())
        if commands:
            return commands[tool.group("verb").lower()]
    bare = _BARE_COMMAND_RE.match(raw)
    if bare:
        return bare.group("command").strip()
    install = _INSTALL_WITH_RE.match(raw)
    if install:
        tool = (install.group("tool") or install.group("tool2")).lower()
        packages = (install.group("packages") or install.group("packages2")).strip()
        return f"{tool} install {packages}"
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


def windows_run_request_command(text: str) -> str:
    """On Windows, the command line a whole-message run request names ("run python calc.py in <folder>"),
    or "" when the message is not one, and always off Windows (the write gate's seam).

    Hardware M4 round 3 (0.7.1): such a request is answered with what the command printed. The
    bounded builder lane does not claim it (it answered "I completed 1 bounded builder step under
    `generated/...`" without the output), and the tool loop ends on the run's own output with a
    written line instead of asking the model for another round."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return ""
    return requested_command(text)


def windows_program_request_refusal(text: str, *, source_context: dict | None = None) -> str:
    """On Windows, the refusal a run request gets because its program never runs there, or because the
    folder it names to run in is outside the marked folders (`windows_run_folder_refusal`); "" when
    the message is not a run request, its command and folder would pass the pre-decision checks, or
    off Windows."""
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
    if refusal is not None:
        return str(refusal.response_text or "")
    return windows_run_folder_refusal(text, source_context=source_context)


def windows_run_folder_refusal(text: str, *, source_context: dict | None = None) -> str:
    """On Windows, the refusal for "run <command> in <folder>" whose folder is outside every marked
    folder and outside the chat's own folder; "" otherwise, and always off Windows.

    Hardware M4 (0.7.1): the folder after " in " is the job's working folder
    (`core.execution.planner._windows_command_in_folder`). A folder the sandbox would refuse as its
    working folder (outside the chat's folder, `sandbox_cwd_refusal`) that no mark covers either is
    answered here, before any card, in words about the marks. Before, the same ask was answered by
    the directory lane as "I can only inspect safe local directories in this lane". Nothing is
    allowed here: a folder this does not refuse is still decided by every gate downstream."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return ""
    import os

    from core.execution.planner import _windows_command_in_folder
    from core.execution.write_demand import verbatim_request_text

    try:
        command, folder = _windows_command_in_folder(verbatim_request_text(source_context, text) or text)
        if not folder:
            return ""
        # Where the folder really is, links and junctions followed, as the marked-folder lane reads it.
        if windows_write_gate.inside_flagged(os.path.realpath(folder)):
            return ""
        from core.runtime_execution_tools import _workspace_root, sandbox_cwd_refusal

        if sandbox_cwd_refusal(folder, workspace_root=_workspace_root(source_context)) is None:
            return ""
    except Exception:  # an unknown state refuses nothing here: the gates downstream still decide
        return ""
    return (
        f"`{folder}` is outside the folders you marked in Settings, Folders VOOL may change, so I won't "
        f"run `{command}` there. Nothing was run."
    )


__all__ = [
    "requested_command",
    "windows_program_request_refusal",
    "windows_run_folder_refusal",
    "windows_run_request_command",
]
