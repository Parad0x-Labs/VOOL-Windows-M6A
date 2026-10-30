from __future__ import annotations

import os
import shlex
from pathlib import PurePath, PureWindowsPath

#: Patched by tests to drive the Windows rules on any platform.
_WINDOWS = os.name == "nt"

_NETWORK_BINARIES = {
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "telnet",
    "ssh",
    "scp",
    "rsync",
    "socat",
    "ftp",
    "tftp",
    "http",
    "https",
}

_INTERPRETER_BINARIES = {"python", "python3", "node", "nodejs", "php", "ruby", "perl"}
_PACKAGE_MANAGER_BINARIES = {"npm", "npx", "pnpm", "yarn", "pip", "pip3", "poetry", "cargo", "brew", "git"}
_NETWORK_KEYWORDS = {
    "http://",
    "https://",
    "socket",
    "urllib",
    "requests",
    "http.client",
    "aiohttp",
    "websocket",
    "fetch(",
    "axios",
    "curl",
    "wget",
}
_PACKAGE_MANAGER_NETWORK_SUBCOMMANDS = {
    "npm": {"install", "add", "update", "upgrade", "audit", "publish", "login", "whoami", "view"},
    "npx": {"*"},
    "pnpm": {"install", "add", "update", "up", "audit", "publish", "login", "whoami", "view", "dlx"},
    "yarn": {"install", "add", "upgrade", "up", "audit", "publish", "login", "whoami", "info", "dlx"},
    "pip": {"install", "download", "wheel", "index"},
    "pip3": {"install", "download", "wheel", "index"},
    "poetry": {"install", "add", "update", "publish", "source"},
    "cargo": {"install", "update", "fetch", "publish", "search"},
    "brew": {"install", "update", "upgrade", "tap", "untap", "search", "info"},
    "git": {"clone", "fetch", "pull", "push", "ls-remote", "remote", "submodule"},
}


def _env_unwrapped(argv: list[str]) -> list[str]:
    if not argv:
        return []
    if _basename(argv[0]) != "env":
        return list(argv)
    index = 1
    while index < len(argv):
        token = str(argv[index] or "")
        if "=" in token and not token.startswith("-"):
            index += 1
            continue
        break
    return list(argv[index:])


def _basename(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if _WINDOWS:
        # C:\...\curl.exe. is curl: Windows paths split on either slash, a name loses its trailing
        # dots and spaces, and a PATHEXT extension (.exe, .cmd, ...) names the same program.
        name = PureWindowsPath(value).name.rstrip(". ").lower()
        stem, dot, extension = name.rpartition(".")
        if dot and stem and f".{extension}" in _windows_program_extensions():
            return stem.rstrip(". ")
        return name
    return PurePath(value).name.lower()


_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


def _windows_program_extensions() -> set[str]:
    listed = f"{_DEFAULT_PATHEXT};{os.environ.get('PATHEXT') or ''}"
    return {item.strip().lower() for item in listed.split(";") if item.strip()}


def command_uses_network(argv: list[str]) -> bool:
    if not argv:
        return False
    normalized_argv = _env_unwrapped(argv)
    if not normalized_argv:
        return False
    first = _basename(normalized_argv[0])
    if first in _NETWORK_BINARIES:
        return True
    if first in _PACKAGE_MANAGER_BINARIES and _package_manager_uses_network(normalized_argv):
        return True
    if first in _INTERPRETER_BINARIES:
        joined = " ".join(str(item or "") for item in normalized_argv[1:]).lower()
        if any(keyword in joined for keyword in _NETWORK_KEYWORDS):
            return True
        if any(keyword in joined for keyword in ("pip install", "npm install", "pnpm install", "yarn add", "cargo install", "git clone")):
            return True
    joined = " ".join(normalized_argv)
    return "http://" in joined or "https://" in joined


def parse_command(cmd: str) -> list[str]:
    if _WINDOWS:
        return _split_windows(cmd)
    return shlex.split(cmd, posix=True)


def _split_windows(cmd: str) -> list[str]:
    """Split as Windows reads a command line: a backslash is a path separator, never an escape (the
    POSIX split turned C:\\Users\\me\\python.exe into C:Usersmepython.exe), except in a run that ends
    at a double quote, where 2n backslashes give n and the quote groups, and 2n+1 give n and a literal
    quote. Single quotes group too, as shlex.join writes them, with nothing escaped inside. '#' is an
    ordinary character, as in shlex.split, and an unclosed quote is refused as shlex.split refuses it."""
    argv: list[str] = []
    word: list[str] = []
    in_word = False
    quote = ""
    index = 0
    while index < len(cmd):
        char = cmd[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
            index += 1
            continue
        if char == "\\":
            end = index
            while end < len(cmd) and cmd[end] == "\\":
                end += 1
            count = end - index
            if end < len(cmd) and cmd[end] == '"':
                word.append("\\" * (count // 2))
                if count % 2:
                    word.append('"')
                    end += 1
            else:
                word.append("\\" * count)
            in_word = True
            index = end
            continue
        if char == '"':
            quote = "" if quote else '"'
            in_word = True
        elif char == "'" and not quote:
            quote = "'"
            in_word = True
        elif char.isspace() and not quote:
            if in_word:
                argv.append("".join(word))
                word, in_word = [], False
        else:
            word.append(char)
            in_word = True
        index += 1
    if quote:
        raise ValueError("No closing quotation")
    if in_word:
        argv.append("".join(word))
    return argv


# Global options that sit BEFORE the subcommand and take a value argument.
# Without stripping these, a token like "git -C /tmp clone" pushes the real
# subcommand ("clone") past the window we inspect, letting a network action
# slip through the static guard. We strip the flag AND its value so the
# subcommand is identified positionally as if the flags were absent.
_OPTION_TAKES_VALUE: dict[str, set[str]] = {
    "git": {"-C", "--git-dir", "--work-tree", "--namespace", "-c", "--exec-path"},
    "npm": {"--prefix", "-C", "--cache", "-w", "--workspace"},
    "npx": {"--prefix", "-C", "-p", "--package"},
    "pnpm": {"--prefix", "-C", "--dir", "-w", "--workspace-root", "--filter", "-F"},
    "yarn": {"--cwd"},
    "pip": {"--cache-dir", "--log", "-c", "--config"},
    "pip3": {"--cache-dir", "--log", "-c", "--config"},
    "poetry": {"-C", "--directory"},
    "cargo": {"-C", "--config", "-Z"},
    "brew": {},
}


def _strip_leading_global_options(base: str, tokens: list[str]) -> list[str]:
    """Drop leading global flags (and any value they consume) so the positional
    subcommand can be found regardless of pre-subcommand options."""
    takes_value = _OPTION_TAKES_VALUE.get(base, set())
    index = 0
    n = len(tokens)
    while index < n:
        token = tokens[index]
        if not token.startswith("-"):
            break
        # "--flag=value" carries its own value; consume only this token.
        if "=" in token:
            index += 1
            continue
        if token in takes_value:
            index += 2  # skip flag and its separate value
            continue
        index += 1  # value-less flag (e.g. "-q", "--quiet")
    return tokens[index:]


def _package_manager_uses_network(argv: list[str]) -> bool:
    if not argv:
        return False
    base = _basename(argv[0])
    if base not in _PACKAGE_MANAGER_BINARIES:
        return False
    raw = [str(item or "").strip() for item in argv[1:] if str(item or "").strip()]
    if not raw:
        return False
    positional = _strip_leading_global_options(base, raw)
    subcommands = [token.lower() for token in positional]
    if not subcommands:
        return False
    if base == "git" and subcommands[:1] == ["remote"]:
        return any(token in {"add", "set-url"} for token in subcommands[1:2])
    wanted = _PACKAGE_MANAGER_NETWORK_SUBCOMMANDS.get(base) or set()
    if "*" in wanted:
        return True
    return any(token in wanted for token in subcommands[:2])
