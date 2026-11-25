"""Repository metadata the generic write tools may not change.

A repository's ``.git`` entry (a directory, or a file pointing at one) holds configuration and hooks that git runs
later for the user, outside any sandbox. The command sandbox already denies writes to it (sandbox.job_runner's
``_SEATBELT_GIT_METADATA_FILTER``); this module is the same rule for the file tools, asked through
``core.runtime_state_protection.refusal`` on every write.

A path is refused when any of its components is ``.git`` in any letter case (macOS volumes are case-insensitive by
default), compared both as written (made absolute) and with symlinks resolved, or when it lies inside a directory
that git itself would treat as a repository directory, whatever that directory is called.

A repository's configuration can also send git to ordinary project files: `core.hooksPath` names the hooks folder
(Husky sets it to a folder in the project) and `include.path` / `includeIf.*.path` pull in more configuration.
For a path inside a working tree, git itself is asked where its hooks folder and every configuration file it reads
are, and a write there is refused too, as is any file in the working tree named on the command line of a setting
that makes git run a program (such as `core.fsmonitor` or a filter or diff driver). When git cannot answer and the repository's own configuration names either
setting, the write is refused rather than guessed.

Windows (port): a ``.git`` name is also matched the way NTFS reads it (trailing dots and spaces dropped, an NTFS
stream suffix such as ``.git::$INDEX_ALLOCATION``, the 8.3 short name ``git~1``: git's ``is_ntfs_dotgit``), a
folder with ``HEAD`` and a ``commondir`` file counts as a git folder, and a folder that cannot be read is refused.
VOOL runs no git on Windows (core.safe_git), so git is never asked there: a repository whose own configuration
names a hooks folder or an include is refused outright (the "git cannot answer" rule below).
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

REFUSAL_PREFIX = "protected_git_metadata"

# Code points HFS+ ignores in file names, so a name carrying them is still `.git` there (git's is_hfs_dotgit list).
_HFS_IGNORABLE = frozenset(
    "‌‍‎‏‪‫‬‭‮⁪⁫⁬⁭⁮⁯﻿"
)


_SHORT_NAME = re.compile(r"git~\d+")


def is_git_metadata_name(name: str) -> bool:
    """Whether a single path component names a ``.git`` entry on some filesystem git supports: HFS+ ignorable code
    points dropped (git's is_hfs_dotgit), case-folded, and as NTFS reads it (is_ntfs_dotgit: trailing dots and
    spaces dropped, a stream suffix after ``:``, the 8.3 short name ``git~1``)."""
    plain = "".join(ch for ch in name if ch not in _HFS_IGNORABLE)
    folded = plain.split(":", 1)[0].rstrip(" .").casefold()
    return plain.casefold() == ".git" or folded == ".git" or bool(_SHORT_NAME.fullmatch(folded))


_is_dotgit = is_git_metadata_name


def _forms(path: Path) -> list[Path]:
    forms: list[Path] = []
    with contextlib.suppress(OSError, ValueError):
        forms.append(Path(os.path.abspath(path)))
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = path.resolve()
        if all(str(resolved) != str(form) for form in forms):
            forms.append(resolved)
    return forms


def _is_git_directory(directory: Path) -> bool:
    # git's own test for a repository directory (setup.c is_git_directory): HEAD, plus objects/ and refs/ or a
    # commondir file standing for them. A folder that cannot be read cannot be ruled out: refuse.
    try:
        if not (directory / "HEAD").is_file():
            return False
        if (directory / "commondir").is_file():
            return True
        return (directory / "objects").is_dir() and (directory / "refs").is_dir()
    except OSError:
        return True


_GIT_TIMEOUT_SECONDS = 10.0
# One write asks several times (each check, each path of a patch); git's answer is reused for this long.
_CACHE_SECONDS = 2.0
_TARGETS_CACHE: dict[str, tuple[float, tuple[list[Path], list[Path]] | None]] = {}


def _worktree_top(form: Path) -> Path | None:
    for directory in form.parents:
        with contextlib.suppress(OSError):
            if os.path.lexists(directory / ".git"):
                return directory
    return None


def _local_config_names_redirect(top: Path) -> bool:
    """Whether the repository's own config text mentions a hooks folder or an include (read without git)."""
    entry = top / ".git"
    try:
        if entry.is_file():
            pointer = entry.read_text(encoding="utf-8", errors="replace").strip()
            gitdir = Path(pointer.split(":", 1)[1].strip()) if pointer.lower().startswith("gitdir:") else None
            if gitdir is None:
                return True
            config = (gitdir if gitdir.is_absolute() else top / gitdir) / "config"
        else:
            config = entry / "config"
        text = config.read_text(encoding="utf-8", errors="replace").casefold() if config.exists() else ""
    except OSError:
        return True
    return "hookspath" in text or "include" in text


def _git(top: Path, *args: str, timeout: float) -> str | None:
    from core import safe_git

    # Windows: VOOL starts no git (core.safe_git refuses it), so git cannot answer and the caller falls back to the
    # repository's own configuration text (refused when it names a hooks folder or an include).
    git = None if safe_git.git_refused_here() else shutil.which("git")
    if not git:
        return None
    # VOOL's own GIT_* variables (GIT_DIR, GIT_CONFIG_*) would make git describe some other repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        done = subprocess.run([git, "-C", str(top), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# Settings whose value is a command line git runs.
_PROGRAM_KEYS = frozenset({
    "core.fsmonitor", "core.pager", "core.editor", "core.sshcommand", "core.askpass", "sequence.editor",
    "credential.helper", "gpg.program", "gpg.openpgp.program", "gpg.x509.program", "gpg.ssh.program",
    "diff.external", "difftool.cmd", "mergetool.cmd", "pager.log", "web.browser", "sendemail.smtpserver",
    "interactive.difffilter", "core.gitproxy", "core.alternaterefscommand", "sendemail.sendmailcmd",
})
_PROGRAM_SUFFIXES = (".clean", ".smudge", ".process", ".command", ".textconv", ".driver", ".cmd", ".helper",
                     ".program", ".path", ".receivepack", ".uploadpack", ".proxy", ".sshcommand", ".update")


def _runs_a_program(key: str) -> bool:
    if key in _PROGRAM_KEYS or key.startswith(("alias.", "pager.")):
        return True
    return "." in key and key.startswith(("filter.", "diff.", "merge.", "difftool.", "mergetool.", "credential.",
                                          "remote.", "core.", "gpg.", "browser.", "man.", "trailer.", "submodule.")) and key.endswith(_PROGRAM_SUFFIXES)


def _redirect_targets(top: Path) -> tuple[list[Path], list[Path]] | None:
    """(hooks folders, configuration files) git uses for the working tree at ``top``, or None if git cannot say."""
    cached = _TARGETS_CACHE.get(str(top))
    if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    started = time.monotonic()
    hooks_out = _git(top, "rev-parse", "--path-format=absolute", "--git-path", "hooks", timeout=_GIT_TIMEOUT_SECONDS)
    remaining = _GIT_TIMEOUT_SECONDS - (time.monotonic() - started)
    config_out = _git(top, "config", "--list", "--show-origin", "-z", timeout=remaining) if hooks_out is not None and remaining > 0 else None
    if hooks_out is None or config_out is None:
        _TARGETS_CACHE[str(top)] = (time.monotonic(), None)
        return None
    hooks = [Path(hooks_out.strip())] if hooks_out.strip() else []
    files: list[Path] = []
    # `--show-origin -z` emits: origin NUL key NEWLINE value NUL, repeated.
    parts = config_out.split("\0")
    for index in range(0, len(parts) - 1, 2):
        origin, entry = parts[index], parts[index + 1]
        key, _sep, value = entry.partition("\n")
        origin_file = Path(origin[5:]) if origin.startswith("file:") else None
        if origin_file is not None:
            files.append(origin_file if origin_file.is_absolute() else top / origin_file)
        lowered = key.casefold()
        if value and (lowered == "include.path" or (lowered.startswith("includeif.") and lowered.endswith(".path"))):
            named = Path(value).expanduser()
            base = (files[-1].parent if origin_file is not None else top)
            files.append(named if named.is_absolute() else base / named)
        elif value and lowered == "core.hookspath":
            named = Path(value).expanduser()
            hooks.append(named if named.is_absolute() else top / named)
        elif value and _runs_a_program(lowered):
            # A setting that makes git run a program (core.fsmonitor, a filter or diff driver, ...): any file in the
            # working tree its command line names is git's to run, not the file tools' to change -- named now or later.
            for token in value.replace('"', " ").replace("'", " ").split():
                if token.startswith("-") or token.startswith("!"):
                    token = token.lstrip("!")
                    if not token or token.startswith("-"):
                        continue
                named = Path(token).expanduser()
                named = named if named.is_absolute() else top / named
                with contextlib.suppress(OSError, ValueError):
                    if named.resolve() == top.resolve() or top.resolve() in named.resolve().parents:
                        files.append(named)
    result = (hooks, files)
    _TARGETS_CACHE[str(top)] = (time.monotonic(), result)
    return result


def _local_config_files(top: Path) -> tuple[list[Path], Path | None] | None:
    """(config files to read first, a linked worktree's own config.worktree) found without git, as git finds them.

    A `.git` file points at the git directory; a linked worktree's git directory holds a `commondir` file that names
    the repository's own folder, whose `config` is the one git reads (plus the worktree's `config.worktree` when
    extensions.worktreeConfig is on). None (refuse) when a pointer cannot be read or the config it leads to is
    missing: a missing file must never read as "no settings"."""
    entry = top / ".git"
    try:
        if entry.is_file():
            pointer = entry.read_text(encoding="utf-8", errors="replace").strip()
            if not pointer.lower().startswith("gitdir:"):
                return None
            gitdir = Path(pointer.split(":", 1)[1].strip())
            gitdir = gitdir if gitdir.is_absolute() else top / gitdir
        else:
            gitdir = entry
        common = gitdir
        commondir = gitdir / "commondir"
        if commondir.is_file():
            named = Path(commondir.read_text(encoding="utf-8", errors="replace").strip())
            common = named if named.is_absolute() else gitdir / named
        config = common / "config"
        if not config.is_file():
            return None
        worktree = gitdir / "config.worktree" if common != gitdir else common / "config.worktree"
        return [config], worktree
    except OSError:
        return None


_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(.*)$')
_ENTRY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")
_MAX_INCLUDES = 16


def _protected_target(named: Path, top: Path) -> bool:
    """A file a program setting names that file tools could reach: in this tree, or in any folder the Windows
    write gate has marked (a script in another marked workspace is git's to run just the same)."""
    with contextlib.suppress(OSError, ValueError):
        if named.resolve() == top.resolve() or top.resolve() in named.resolve().parents:
            return True
    from core import windows_write_gate

    return windows_write_gate.inside_flagged(str(named))


def _global_config_files() -> list[Path]:
    """The user's global git config files, as Git for Windows finds them (HOME, else USERPROFILE; XDG)."""
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    found: list[Path] = []
    if home:
        found.append(Path(home) / ".gitconfig")
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    if xdg:
        found.append(Path(xdg) / "git" / "config")
    elif home:
        found.append(Path(home) / ".config" / "git" / "config")
    return found


def _cut_comment(raw: str) -> str | None:
    """A config value as git reads it: cut at the first `;` or `#` outside double quotes, quotes dropped, `\\x`
    kept as written. None (refuse) for a value continued on the next line or a quote left open."""
    out: list[str] = []
    quoted = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\":
            if index + 1 >= len(raw):
                return None
            out.append(raw[index:index + 2])
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in ";#" and not quoted:
            break
        else:
            out.append(char)
        index += 1
    else:
        if quoted:
            return None
    return "".join(out).strip()


_WINDOWS_PROGRAM_SUFFIXES = (".com", ".exe", ".bat", ".cmd")


def _program_spellings(named: Path) -> list[Path]:
    """The files Windows may run for a command word: as written and, for a word without a suffix, with each
    PATHEXT suffix (CreateProcess and Git for Windows' sh add them)."""
    if named.suffix:
        return [named]
    suffixes = {suffix.casefold() for suffix in _WINDOWS_PROGRAM_SUFFIXES}
    suffixes.update(part.strip().casefold() for part in os.environ.get("PATHEXT", "").split(";")
                    if part.strip().startswith("."))
    return [named, *(named.with_name(named.name + suffix) for suffix in sorted(suffixes))]


def _local_targets(top: Path) -> tuple[list[Path], list[Path]] | None:
    """Windows port: (hooks folders, files) as `_redirect_targets` gives them, read from the repository's own config.

    git does not run on Windows (core.safe_git refuses it), so git cannot list them. This reads the local config and
    every file it includes (an includeIf is followed whatever its condition, so more is protected, never less), split
    the way git's parser splits section, subsection and key: core.hooksPath, include paths, and each word of a program
    setting's command line, as `_redirect_targets` takes them. None (refuse) when a file cannot be read, a line cannot
    be placed, or includes go deeper than _MAX_INCLUDES."""
    found = _local_config_files(top)
    if found is None:
        return None
    queue, worktree_config = found
    if worktree_config is not None and worktree_config.is_file():
        # git reads config.worktree when extensions.worktreeConfig is on; read it whenever it is there (more is
        # protected, never less).
        queue.append(worktree_config)
    # The user's own global settings apply to every repository (a global core.hooksPath = .githooks is common).
    # One that is not there names nothing; the system config is not read (a 0.7.7 item).
    queue.extend(path for path in _global_config_files() if path.is_file())
    hooks: list[Path] = []
    files: list[Path] = [worktree_config] if worktree_config is not None else []
    seen: set[str] = set()
    while queue:
        config = queue.pop()
        if len(seen) >= _MAX_INCLUDES:
            return None
        key_seen = str(config.resolve()) if config.exists() else str(config)
        if key_seen in seen:
            continue
        seen.add(key_seen)
        files.append(config)
        try:
            text = config.read_text(encoding="utf-8-sig", errors="replace") if config.exists() else ""
        except OSError:
            return None
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            match = _SECTION.match(line)
            if match:
                name, sub, rest = match.group(1), match.group(2), match.group(3).strip()
                section = (name.casefold() + "." + sub.replace('\\"', '"')) if sub is not None else name.casefold()
                if not rest or rest[0] in "#;":
                    continue
                line = rest
            entry = _ENTRY.match(line)
            if entry is None or not section:
                return None
            value = _cut_comment(entry.group(2) or "")
            if value is None:
                return None  # a value continued on the next line, or an open quote: not placed here, refuse
            key = (section + "." + entry.group(1)).casefold()
            if not value:
                continue
            if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
                named = Path(value).expanduser()
                queue.append(named if named.is_absolute() else config.parent / named)
            elif key == "core.hookspath":
                named = Path(value).expanduser()
                hooks.append(named if named.is_absolute() else top / named)
            elif _runs_a_program(key):
                for token in value.replace('"', " ").replace("'", " ").split():
                    token = token.lstrip("!")
                    if not token or token.startswith(("-", "#", ";")):
                        continue
                    named = Path(token).expanduser()
                    named = named if named.is_absolute() else top / named
                    for program in _program_spellings(named):
                        if _protected_target(program, top):
                            files.append(program)
    return hooks, files


def _redirect_refusal(candidate: Path, forms: list[Path], top: Path) -> str:
    targets = _redirect_targets(top)
    if targets is None:
        if _local_config_names_redirect(top):
            return (f"{REFUSAL_PREFIX}: git could not say where the repository at `{top}` keeps its hooks and included "
                    f"settings, and its configuration names them, so `{candidate}` was not written; nothing was touched.")
        from core import safe_git

        if not safe_git.git_refused_here():
            return ""
        # Windows port: git never runs here, so a script that a program setting in the repository's own config
        # names (core.fsmonitor, a filter, core.gitProxy, ...) would go unprotected. VOOL reads that config itself.
        targets = _local_targets(top)
        if targets is None:
            return (f"{REFUSAL_PREFIX}: VOOL could not read the settings of the repository at `{top}`, so "
                    f"`{candidate}` was not written; nothing was touched.")
    return _target_refusal(candidate, forms, *targets)


def _target_refusal(candidate: Path, forms: list[Path], hooks: list[Path], files: list[Path]) -> str:
    for target in forms:
        for folder in hooks:
            for folder_form in _forms(folder):
                if target == folder_form or folder_form in target.parents:
                    return (f"{REFUSAL_PREFIX}: `{candidate}` is in the hooks folder git uses for this repository. File "
                            "tools never change a repository's hooks; nothing was touched.")
        for config in files:
            if any(target == config_form for config_form in _forms(config)):
                return (f"{REFUSAL_PREFIX}: `{candidate}` is a configuration file git reads for this repository. File "
                        "tools never change a repository's own configuration; nothing was touched.")
    return ""


def refusal(path: str | Path) -> str:
    """'' when a generic tool may write ``path``; otherwise the refusal to report (nothing was touched)."""
    candidate = Path(str(path)).expanduser()
    for form in _forms(candidate):
        if any(_is_dotgit(part) for part in form.parts):
            return (f"{REFUSAL_PREFIX}: `{candidate}` is inside a repository's .git metadata. File tools never change "
                    "a repository's own configuration or hooks; nothing was touched.")
        for directory in (form, *form.parents):
            if _is_git_directory(directory):
                return (f"{REFUSAL_PREFIX}: `{candidate}` is inside a git repository directory. File tools never "
                        "change a repository's own configuration or hooks; nothing was touched.")
    forms = _forms(candidate)
    tops = {str(top): top for top in (_worktree_top(form) for form in forms) if top is not None}
    for top in tops.values():
        reason = _redirect_refusal(candidate, forms, top)
        if reason:
            return reason
    return _marked_repositories_refusal(candidate, forms, set(tops))


def _marked_repositories_refusal(candidate: Path, forms: list[Path], checked: set[str]) -> str:
    """Windows port: a program setting of a repository in one marked folder may name a script in another one.

    The check above asks only the repository the written file sits in. Where git never runs (Windows), the
    repositories at the marked folders (the folder itself, or the repository holding it) are asked too, so a
    script one of them would run is refused wherever it lies."""
    from core import safe_git, windows_write_gate

    if not safe_git.git_refused_here():
        return ""
    try:
        roots = windows_write_gate.flagged_roots()
    except Exception:  # an unknown state flags nothing, and nothing is written there either
        return ""
    for root in roots:
        folder = Path(root)
        top = folder if os.path.lexists(folder / ".git") else _worktree_top(folder / "_")
        if top is None or str(top) in checked:
            continue
        checked.add(str(top))
        # Only what that repository names is matched here; its hooks-folder or include rule refuses writes inside
        # it (above), not everywhere. A repository whose settings cannot be read names nothing to match.
        targets = _local_targets(top)
        if targets is not None:
            reason = _target_refusal(candidate, forms, *targets)
            if reason:
                return reason
    return ""


__all__ = ["REFUSAL_PREFIX", "is_git_metadata_name", "refusal"]
