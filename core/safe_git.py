"""Run git on a repository VOOL did not author without letting that repository run programs.

A repository's own local config can make git execute arbitrary programs: ``core.fsmonitor`` runs
on ``git status``, ``filter.<x>.clean/process`` runs when status compares a racily-clean file,
``diff.<x>.textconv/command`` and ``diff.external`` run on ``git diff``, a local-path remote's
``uploadpack``/``receivepack`` runs on fetch/push, and hooks such as ``post-index-change`` run
when status refreshes the index. Reproduced 2026-10-18: VOOL's ``workspace.git_status`` ran a
program named by a repo's ``core.fsmonitor``, with no sandbox and no approval, on Windows and on
every other platform.

Three layers:
- every invocation neutralises what can be overridden from the command line (no optional locks,
  so no index refresh or hooks; fsmonitor off; hooks pointed at a path that does not exist; no
  external diff or textconv; a plain pager);
- every invocation runs with a scrubbed environment: no inherited ``GIT_*`` variable except the
  author/committer identity, so ``GIT_CONFIG_COUNT``/``GIT_CONFIG_PARAMETERS`` cannot inject a
  program and ``GIT_DIR``/``GIT_INDEX_FILE`` cannot redirect git away from the checked repository;
- a repository whose LOCAL config (includes expanded) defines any key of a program-running KIND
  (by its final name segment, so new keys of a known kind are caught too) is refused before git
  touches it.
System and global config belong to the user (for example Git LFS's global filters) and are not
second-guessed. Residual, documented rather than claimed closed: a process editing .git/config
between the check and the run (the same check/use class as the Windows filesystem race).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

#: Final key segments (lowercase) whose value git runs as a program or shell command.
_EXECUTABLE_KEY_SUFFIXES = frozenset({
    "command",            # diff.<x>.command, core.sshCommand, core.alternateRefsCommand, ...
    "sshcommand", "alternaterefscommand", "gitproxy",
    "program",            # gpg.program, gpg.<fmt>.program
    "editor",             # core.editor, sequence.editor
    "askpass",            # core.askPass
    "pager",              # core.pager, pager.<cmd>
    "external",           # diff.external
    "textconv",
    "clean", "smudge", "process",   # filter.<x>.*
    "driver",             # merge.<x>.driver
    "helper",             # credential.helper, credential.<url>.helper
    "uploadpack", "receivepack",    # remote.<x>.*: run locally for local-path remotes
    "fsmonitor", "hookspath",
})
#: Whole keys (fnmatch, lowercase) that pull in configuration git would then obey.
_EXECUTABLE_KEY_PATTERNS = ("include.path", "includeif.*.path", "pager.*")
#: Inherited environment variables git may keep: identity only.
_ALLOWED_GIT_ENV = frozenset({
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})


def is_executable_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered.rsplit(".", 1)[-1] in _EXECUTABLE_KEY_SUFFIXES:
        return True
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _EXECUTABLE_KEY_PATTERNS)


def safe_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a hardened git child: inherited GIT_* scrubbed except identity, and git
    never prompts on a terminal."""
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items()
           if not key.upper().startswith("GIT_") or key.upper() in _ALLOWED_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def hardening_args() -> list[str]:
    """Global git options that keep a command from running repository-chosen programs."""
    return [
        "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "diff.external=",
        "-c", "core.pager=cat",
    ]


def executable_local_config(cwd: Path, *, timeout: float = 20) -> list[str]:
    """The program-running keys the repository's own config defines (empty when none)."""
    probe = subprocess.run(
        ["git", "-C", str(cwd), "config", "--local", "--includes", "--null", "--list"],
        capture_output=True, timeout=timeout, env=safe_env(),
    )
    if probe.returncode not in (0, 1):  # 1: no local config entries at all
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    keys = []
    for entry in probe.stdout.split(b"\0"):
        if not entry:
            continue
        key = entry.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()
        if is_executable_key(key):
            keys.append(key)
    return sorted(set(keys))


def active_hooks(cwd: Path, *, timeout: float = 20) -> list[str]:
    """Hooks git would run here without hardening (the repository's own, or the user's
    core.hooksPath). Hardening disables them; a mutation that would have run one is refused
    instead, so a pre-commit check is never skipped silently."""
    probe = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(cwd), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True, timeout=timeout, env=safe_env(),
    )
    if probe.returncode != 0:
        raise RuntimeError(f"git hooks directory could not be resolved ({probe.returncode})")
    hooks = Path(probe.stdout.strip())
    if not hooks.is_absolute():
        hooks = Path(cwd) / hooks
    if not hooks.is_dir():
        return []
    return sorted(entry.name for entry in hooks.iterdir() if entry.is_file() and not entry.name.endswith(".sample"))


def hardened_argv(cwd: Path, *argv: str) -> list[str]:
    """A hardened ``git -C <cwd> <argv>``. Diff-family commands also get ``--no-ext-diff --no-textconv``.
    Run it with ``env=safe_env()``."""
    command = ["git", *hardening_args(), "-C", str(cwd), *argv]
    if argv and argv[0] in {"diff", "log", "show"}:
        command[len(command) - len(argv) + 1:len(command) - len(argv) + 1] = ["--no-ext-diff", "--no-textconv"]
    return command


read_only_argv = hardened_argv


__all__ = ["active_hooks", "executable_local_config", "hardened_argv", "hardening_args", "is_executable_key",
           "read_only_argv", "safe_env"]
