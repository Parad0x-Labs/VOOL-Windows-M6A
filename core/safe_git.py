"""Run git on a repository VOOL did not author without letting that repository run programs.

A repository's own local config can make git execute arbitrary programs: ``core.fsmonitor`` runs
on ``git status``, ``filter.<x>.clean/process`` runs when status compares a racily-clean file,
``diff.<x>.textconv/command`` and ``diff.external`` run on ``git diff``, and hooks such as
``post-index-change`` run when status refreshes the index. Reproduced 2026-10-18: VOOL's
``workspace.git_status`` ran a program named by a repo's ``core.fsmonitor``, with no sandbox and
no approval, on Windows and on every other platform.

Two layers:
- every invocation neutralises what can be overridden from the command line (no optional
  locks, so no index refresh or hooks; fsmonitor off; hooks pointed at a path that does not
  exist; no external diff or textconv);
- a repository whose LOCAL config (includes expanded) defines a program-running key that
  cannot be overridden by name is refused before git touches its working tree.
System and global config belong to the user (for example Git LFS's global filters) and are not
second-guessed.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

#: Local-config keys (lowercase, fnmatch) naming a program git would run on read-only commands.
EXECUTABLE_LOCAL_KEYS = (
    "core.fsmonitor",
    "core.hookspath",
    "core.pager",
    "core.editor",
    "core.sshcommand",
    "core.gitproxy",
    "diff.external",
    "diff.*.textconv",
    "diff.*.command",
    "filter.*.clean",
    "filter.*.smudge",
    "filter.*.process",
    "merge.*.driver",
    "gpg.program",
    "gpg.*.program",
    "credential.helper",
    "credential.*.helper",
    "include.path",
    "includeif.*.path",
)


def hardening_args() -> list[str]:
    """Global git options that keep a read-only command from running repository-chosen programs."""
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
        capture_output=True, timeout=timeout,
    )
    if probe.returncode not in (0, 1):  # 1: no local config entries at all
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    keys = []
    for entry in probe.stdout.split(b"\0"):
        if not entry:
            continue
        key = entry.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in EXECUTABLE_LOCAL_KEYS):
            keys.append(key)
    return sorted(set(keys))


def active_hooks(cwd: Path, *, timeout: float = 20) -> list[str]:
    """Hooks git would run here without hardening (the repository's own, or the user's
    core.hooksPath). Hardening disables them; a mutation that would have run one is refused
    instead, so a pre-commit check is never skipped silently."""
    probe = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(cwd), "rev-parse", "--git-path", "hooks"],
        capture_output=True, text=True, timeout=timeout,
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
    """A hardened ``git -C <cwd> <argv>``. Diff-family commands also get ``--no-ext-diff --no-textconv``."""
    command = ["git", *hardening_args(), "-C", str(cwd), *argv]
    if argv and argv[0] in {"diff", "log", "show"}:
        command[len(command) - len(argv) + 1:len(command) - len(argv) + 1] = ["--no-ext-diff", "--no-textconv"]
    return command


read_only_argv = hardened_argv


__all__ = ["EXECUTABLE_LOCAL_KEYS", "active_hooks", "executable_local_config", "hardened_argv", "hardening_args",
           "read_only_argv"]
