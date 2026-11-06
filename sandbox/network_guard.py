from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import PurePath, PureWindowsPath

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
    return PurePath(value).name.lower()


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


#: Patched by tests to drive the Windows rules on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"

#: Characters cmd.exe gives a meaning outside double quotes. No command is run through a shell, but a
#: command line that relies on one of them is not one VOOL can read the way the user meant, so it is
#: refused rather than guessed at.
_CMD_METACHARACTERS = frozenset('&|<>^()%!')
_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class CommandLineRefused(ValueError):
    """A command line this platform's rules refuse to split. A ValueError, like shlex's own."""


def parse_command(cmd: str) -> list[str]:
    """Split a command line into argv by the rules of the platform that will run it.

    POSIX: shell words (`shlex`, posix mode). Windows: the CommandLineToArgvW rules, so a backslash
    is a path separator and not an escape. There, a cmd.exe metacharacter outside double quotes, a
    line break, or an argv[0] that names a path but is not an absolute `.exe` raises
    `CommandLineRefused`."""
    if not _IS_WINDOWS:
        return shlex.split(cmd, posix=True)
    return _windows_argv(str(cmd or ""))


def require_windows_exe(program: str, *, which=None) -> str:
    """On Windows, the path of the program that would actually run, which must be a `.exe`.

    A bare name is resolved the way Windows finds it (PATH and PATHEXT). A name that resolves to a
    `.cmd` or `.bat` runs through cmd.exe, which re-reads the whole line, metacharacters inside quotes
    included, so it is refused, as is a name that resolves to nothing. Off Windows, `program` is
    returned unchanged."""
    if not _IS_WINDOWS:
        return program
    resolved = (which or shutil.which)(program)
    if not resolved:
        raise CommandLineRefused(f"the program {program!r} was not found")
    if PureWindowsPath(str(resolved)).suffix.lower() != ".exe":
        raise CommandLineRefused(
            f"the program {program!r} runs as {resolved!r}, which is not an .exe; "
            "a .cmd or .bat would run through cmd.exe"
        )
    return str(resolved)


def join_command(argv: list[str]) -> str:
    """The inverse of `parse_command` on this platform: `parse_command(join_command(a)) == a`."""
    if not _IS_WINDOWS:
        return shlex.join(argv)
    return subprocess.list2cmdline([str(arg) for arg in argv])


def _windows_argv(line: str) -> list[str]:
    if "\r" in line or "\n" in line:
        raise CommandLineRefused("a Windows command line cannot span lines")
    argv: list[str] = []
    i, n = 0, len(line)
    while i < n and line[i] in " \t":
        i += 1
    if i >= n:
        return []
    # argv[0]: no backslash processing; a double quote only opens and closes a quoted span.
    first: list[str] = []
    quoted = False
    while i < n and (quoted or line[i] not in " \t"):
        ch = line[i]
        if ch == '"':
            quoted = not quoted
        else:
            if not quoted and ch in _CMD_METACHARACTERS:
                raise CommandLineRefused(f"{ch!r} outside double quotes is a cmd.exe metacharacter")
            first.append(ch)
        i += 1
    if quoted:
        raise CommandLineRefused("unbalanced double quote in the program name")
    argv.append("".join(first))
    # The remaining arguments: 2n backslashes before a quote give n and toggle quoting; 2n+1 give n
    # and a literal quote; backslashes before anything else are literal. "" inside quotes is a quote.
    while True:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        arg: list[str] = []
        quoted = False
        while i < n and (quoted or line[i] not in " \t"):
            ch = line[i]
            if ch == "\\":
                start = i
                while i < n and line[i] == "\\":
                    i += 1
                count = i - start
                if i < n and line[i] == '"':
                    arg.append("\\" * (count // 2))
                    if count % 2:
                        arg.append('"')
                        i += 1
                    continue
                arg.append("\\" * count)
                continue
            if ch == '"':
                if quoted and i + 1 < n and line[i + 1] == '"':
                    arg.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            if not quoted and ch in _CMD_METACHARACTERS:
                raise CommandLineRefused(f"{ch!r} outside double quotes is a cmd.exe metacharacter")
            arg.append(ch)
            i += 1
        if quoted:
            raise CommandLineRefused("unbalanced double quote")
        argv.append("".join(arg))
    program = argv[0]
    if any(sep in program for sep in ("\\", "/", ":")):
        if not _DRIVE_ABSOLUTE_RE.match(program) or PureWindowsPath(program).suffix.lower() != ".exe":
            raise CommandLineRefused(
                f"the program {program!r} names a path, so it must be an absolute path to an .exe"
            )
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
