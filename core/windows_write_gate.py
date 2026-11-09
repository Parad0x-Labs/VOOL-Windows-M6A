"""Windows: user-filesystem writes run only inside explicitly flagged disposable dev workspaces.

Until the handle-based Windows filesystem authority exists, a reviewed write on Windows is
checked by path and then performed by path, and that window can be raced, so it is not
confinement. Owner decision (2026-10-18): on Windows, intents that change the user's files are
refused unless every target lies inside a folder listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES
(os.pathsep-separated absolute paths), each path argument checked where its writer puts it
(`_landing`), and every folder the call really runs in checked too: a step's or a code-task rollback's
task or session folder, and for a git operation the top of the repository's work tree, its git folder and
its shared git folder, as git reports them (`_places_beyond_workspace`); a repository whose git folders
hold a link or hard link, or whose own config sets keys that move git's writes (Git LFS keys,
`gc.repackFilterTo`), is refused, since git's writes can then land outside those folders
(core/git_write_redirects.py). An intent that also changes folders its arguments do not name is
refused outright (`_ALWAYS_REFUSED`). A call that runs git is refused outright too, marked folder or
not (`_GIT_INTENTS`): VOOL runs no git on Windows in 0.7.1 (core/safe_git.py).
The developer sets that variable when starting VOOL; nothing inside a
workspace can set it. VOOL's own private stores (skills, media projects, browser profiles,
notes) are not user-filesystem destinations and are not gated here. POSIX is unaffected.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import windows_path_policy

logger = logging.getLogger(__name__)

ENV_VAR = "VOOL_WINDOWS_DISPOSABLE_WORKSPACES"
#: Patched by tests to drive the Windows decision on any platform.
_IS_WINDOWS = os.name == "nt"
STATUS = "windows_filesystem_authority_unavailable"

#: Intents that change the user's filesystem, mapped to the argument keys naming their targets.
#: An empty tuple means the target is the workspace itself. `_landing` places each keyed path;
#: a keyed intent it has no rule for is refused.
_GATED: dict[str, tuple[str, ...]] = {
    "workspace.write_file": (),
    "workspace.ensure_directory": (),
    "workspace.replace_in_file": (),
    "workspace.apply_unified_diff": (),
    "workspace.rollback_last_change": (),
    "code.task.rollback": (),
    "repo.git": (),
    "machine.write_file": ("path",),
    "machine.ensure_directory": ("path",),
    "machine.move_path": ("source", "destination"),
    "operator.move_path": ("source_path", "destination_path"),
    "operator.cleanup_temp_files": ("target_path",),
    "media.export": ("output_path",),
}
#: Keyed intents whose writer changes only the paths their arguments name, placed from the user's
#: home (`anchor_machine_path`) and never from the chat's workspace, so the chat's workspace is not
#: one of their targets. Checked against it as well, a write the owner asked for inside a marked
#: folder was refused whenever the chat's own workspace (an unbound chat's Desktop) was not marked
#: (captest B3, 2026-11-08). Every path they name must still lie inside a marked folder.
_LANDING_ONLY = frozenset({"machine.write_file", "machine.ensure_directory"})
#: Step intents whose effect is the inner intent's (gated only when that inner intent is gated).
_STEPS = frozenset({"code.task.step", "repo.step"})
#: Gated intents refused on Windows whatever they name, because they also change folders their
#: arguments do not name: a temp cleanup empties the folders TMPDIR, TMP and TEMP name, and its
#: approval empties the plan a disk scan stored earlier (core.operator.handlers).
_ALWAYS_REFUSED = frozenset({"operator.cleanup_temp_files"})
#: Intents that run git. VOOL runs no git on Windows in 0.7.1 (core/safe_git.py), so each is refused
#: whatever it names, here, before the permission decision, so no approval is asked for git that can
#: never run, with the same sentence the git runners give. A marked folder does not change that: git
#: also runs a repository's hooks and helpers, which no marked folder confines.
_GIT_INTENTS = frozenset({"workspace.git_status", "workspace.git_diff", "workspace.git_summary"})
#: RepoOps is git's control plane: every repo.* call opens, reads or acts on a git session.
_GIT_INTENT_PREFIXES = ("repo.",)
#: Intents that start a command named in an argument: refused when that command runs git.
_COMMAND_INTENTS: dict[str, tuple[str, ...]] = {"sandbox.run_command": ("command", "cmd")}


#: The skipped entries already warned about, so a list read on every gate check warns once.
_WARNED: set[tuple[str, str]] = set()


def _skip(part: str, reason: str) -> None:
    if (part, reason) not in _WARNED:
        _WARNED.add((part, reason))
        logger.warning("%s entry \"%s\" is not flagged: %s. The other entries stay flagged.", ENV_VAR, part, reason)


#: Environment variables naming folders a marked root may not be, nor hold: as a write root it
#: would open the user's whole profile, the temp folders or the per-user app data (VOOL's own
#: install and home live in LOCALAPPDATA).
_PROFILE_VARS = ("USERPROFILE",)
_HELD_VARS = ("TEMP", "TMP", "LOCALAPPDATA")


#: A drive letter followed by two separators: `C://host/s`, `C:\\\\host\\s`, `C:/\\x`, `C:\\/x`.
_DOUBLED_DRIVE_SEPARATOR_RE = re.compile(r"^[\"'`]?[A-Za-z]:[\\/]{2}")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
#: What `_refused_spelling` answers besides `windows_path_policy.NETWORK_KINDS`.
DOUBLED_DRIVE = "doubled_drive_separator"
REMOTE_DRIVE = "remote_drive"


def _refused_spelling(value: Any) -> str:
    """Why the gate refuses `value` by its spelling alone, before anything resolves, stats or lists
    it, or "" when it may be looked up: a share, a device path or an NT object path
    (`windows_path_policy.classify`, network tier); a drive path with a doubled separator right after
    the colon (`C://host/s`, `C:\\\\host\\s`), which a reader of the text from its first separator
    takes for the share `//host/s`; and a drive Windows does not call local
    (`windows_known_folders._drive_is_remote` is not False: a mapped network drive is a share under
    a letter, and a drive type Windows cannot name counts as remote). Each is judged as written and
    with `%VAR%` and `$VAR` expanded, since the operator lane expands them.

    On Windows looking any of these up is already the SMB connection, and the user's NTLM hash sent
    to whoever answers, so every check here runs before `_inside`, `_landing` or a root's own checks
    reach the filesystem (RECONCILE-351 F1). Asked only where the gate is on."""
    text = windows_path_policy._text(value)
    if text is None:
        return ""
    kind = windows_path_policy.classify(text, network_only=True)
    if kind:
        return kind
    stripped = text.strip()
    for spelled in dict.fromkeys((stripped, ntpath.expandvars(stripped).strip(), os.path.expandvars(stripped).strip())):
        if _DOUBLED_DRIVE_SEPARATOR_RE.match(spelled):
            return DOUBLED_DRIVE
        drive = ntpath.splitdrive(spelled.lstrip("\"'`"))[0]
        if _DRIVE_RE.fullmatch(drive):
            from core import windows_known_folders

            try:
                local = windows_known_folders._drive_is_remote(drive.upper()) is False
            except Exception:  # an unknown drive type counts as remote (fail closed)
                local = False
            if not local:
                return REMOTE_DRIVE
    return ""


def refused_spelling(value: Any, *, windows: bool | None = None) -> str:
    """Where the gate is on (`active`), the kind `_refused_spelling` gives `value`; otherwise "".
    For a lane that reads a path out of a message and must refuse it before its own first lookup."""
    if not active(windows=windows):
        return ""
    return _refused_spelling(value)


def spelling_refusal_text(value: Any, kind: str, *, what: str = "path", nothing: str = "changed") -> str:
    """The sentence the gate refuses a `_refused_spelling` kind with, naming `value` as written."""
    text = windows_path_policy._text(value)
    shown = re.sub(r"[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]", " ",
                   (str(value) if text is None else text).replace("`", ""))[:120]
    if kind == DOUBLED_DRIVE:
        form = "a drive path with a doubled separator, which reads as a share"
    elif kind == REMOTE_DRIVE:
        form = "a drive Windows does not call local (a mapped network drive is a share under a letter)"
    else:
        form = "a share or a device path"
    return (
        f"On Windows, VOOL does not look up the {what} `{shown}`: it is {form}, and looking it up already "
        f"connects to it. Nothing was {nothing}."
    )


def _resolved_norm(value: str) -> str:
    """`value` as the gate compares it: resolved (junctions and links followed) and normcased.
    A share, device, doubled-separator or network-drive spelling is never resolved, since resolving
    it is the SMB connection."""
    if _refused_spelling(value):
        return os.path.normcase(value)
    return os.path.normcase(os.path.realpath(value))


def _holds_path(folder: str, other: str) -> bool:
    """Whether `folder` is `other` or an ancestor of it (both already resolved and normcased)."""
    try:
        return os.path.commonpath([folder, other]) == folder
    except ValueError:  # different drives, or one relative
        return False


def _too_wide(resolved: str) -> str:
    """Why the resolved, normcased folder `resolved` is too wide to be a write root, or '': a drive
    root, the user profile itself, or a folder that is or holds TEMP, TMP or LOCALAPPDATA (a folder
    inside them, such as the test harness's own temp folders, is fine).
    Each named folder is resolved too, so a junction on either side is judged by its target. A
    named folder that cannot be resolved makes the root too wide (unknown states fail closed)."""
    env = os.environ
    if not os.path.splitdrive(resolved)[1].strip("\\/"):
        return "it is a whole drive"
    named: list[tuple[str, str]] = [(name, str(env.get(name) or "").strip()) for name in (*_PROFILE_VARS, *_HELD_VARS)]
    named.append(("the user's home", os.path.expanduser("~")))
    for name, value in named:
        if not value or not os.path.isabs(value):
            continue
        try:
            other = _resolved_norm(value)
        except (OSError, ValueError):
            return f"{name} cannot be resolved, so the folder cannot be checked against it"
        if name in _HELD_VARS:
            if _holds_path(resolved, other):
                return f"it is or holds the {name} folder"
        elif resolved == other:
            # A folder above the profile is skipped too on a real Windows profile, where it
            # also holds LOCALAPPDATA (inside the profile), by the rule above.
            return "it is the user profile"
    return ""


def wide_root_reason(part: str) -> str:
    """Why the folder `part` names may never be a write root on Windows (`_too_wide`), or ''. For
    the broker's list and the owner's list, which read entries outside `_windows_root`."""
    raw = str(part or "").strip()
    if not raw or "\x00" in raw:
        return "it is not a folder path"
    try:
        return _too_wide(_resolved_norm(raw))
    except (OSError, ValueError):
        return "it cannot be resolved"


def _windows_root(part: str, *, wide: bool = True) -> str | None:
    """The flagged root `part` gives on Windows, or None (with a warning) for an entry the broker
    refuses too (Phase C picks 1, 5 and 10, owner 2026-10-23). A share or device path is judged by its
    spelling before anything opens it, because checking whether a share is a folder connects to it.
    On Linux CI every POSIX root spells as root-relative, so the full spelling rule applies only on
    a real Windows host. A drive root, the user profile and a folder holding TEMP, TMP or
    LOCALAPPDATA are skipped too, judged after resolving (`_too_wide`); `wide=False` leaves that
    to the caller (the owner's list judges marks with its own rules, core/windows_disposable_store.py)."""
    kind = _refused_spelling(part) or (
        windows_path_policy.classify(part) if os.name == "nt" else ""
    )
    if kind:
        _skip(part, "it is a network, device or incomplete path")
        return None
    if not os.path.isabs(part):
        _skip(part, "it is not a full path")
        return None
    if os.path.islink(part) or getattr(os.path, "isjunction", lambda _path: False)(part):
        _skip(part, "it is a junction or link; flag the folder it points at instead")
        return None
    if not os.path.isdir(part):
        _skip(part, "it is not an existing folder")
        return None
    resolved = os.path.realpath(part)
    if windows_path_policy.classify(resolved, network_only=True):
        _skip(part, "it is on a network drive")
        return None
    root = os.path.normcase(resolved)
    if wide:
        try:
            too_wide = _too_wide(root)
        except (OSError, ValueError):
            too_wide = "it cannot be checked against the profile and temp folders"
        if too_wide:
            _skip(part, f"{too_wide}, which as a write root would open far more than a workspace")
            return None
    return root


def flagged_roots(environ: dict[str, str] | None = None) -> list[str]:
    """The flagged folders, each as a normalised final path.

    Once this process has started the Windows broker, they are the broker's (docs/WINDOWS_BROKER.md
    section 4, one list), each still at the path the broker reported, and none while it does not
    run; otherwise the variable is read."""
    if environ is None and _IS_WINDOWS:
        from core.windows_broker.session import checked_folders, wired

        if wired():
            folders = [os.path.normcase(path) for path in checked_folders()]
            # An owner unmark shrinks at once, even though the broker's list is fixed at its start.
            gone = _unmarked_since_start()
            if gone:
                kept = set(_env_roots(os.environ))
                from core.windows_disposable_store import normalised

                folders = [path for path in folders
                           if path in kept or ("*" not in gone and normalised(path) not in gone)]
            # The broker's own checks refuse a volume root but not the profile or the temp folders.
            kept_folders = []
            for path in folders:
                too_wide = wide_root_reason(path)
                if too_wide:
                    _skip(path, f"{too_wide}, which as a write root would open far more than a workspace")
                else:
                    kept_folders.append(path)
            return kept_folders
    roots = _env_roots(os.environ if environ is None else environ)
    if environ is None and _IS_WINDOWS:
        # The owner's own list (core/windows_disposable_store.py): its entries as they were at this
        # process's start, shrunk by any unmark since, each through the same root checks.
        for part in _store_start_folders():
            root = _windows_root(part)
            if root is not None and root not in roots:
                roots.append(root)
    return roots


def _env_roots(environ) -> list[str]:
    raw = environ.get(ENV_VAR, "")
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        if _IS_WINDOWS:
            root = _windows_root(part)
            if root is not None:
                roots.append(root)
        elif os.path.isabs(part) and os.path.isdir(part):
            roots.append(os.path.normcase(os.path.realpath(part)))
    return roots


def _store_start_folders() -> list[str]:
    """The owner-marked folders that count in this process; none on any doubt about the file."""
    from core import windows_disposable_store

    try:
        return windows_disposable_store.start_folders()
    except Exception as exc:
        logger.warning("the owner's disposable folder list adds nothing: %s", type(exc).__name__)
        return []


def _unmarked_since_start() -> set[str]:
    from core import windows_disposable_store

    try:
        return windows_disposable_store.unmarked_since_start()
    except Exception as exc:  # an unreadable file means none of its entries count
        logger.warning("the owner's disposable folder list cannot be read: %s", type(exc).__name__)
        return {"*"}


def broker_report() -> dict[str, Any]:
    """The Windows broker's report for GET /api/windows/broker/status (docs/WINDOWS_BROKER.md
    section 8): ``{"wired": False}`` in a process that never started a broker. Kept here so the
    API reads the broker through the gate, the one place that already asks it."""
    from core.windows_broker.session import report

    return report()


def capability_report() -> dict[str, Any] | None:
    """The Windows write rule as GET /api/runtime/capabilities states it, or None when this process
    does not enforce it (`active()`). The folders are `flagged_roots()`, the list every gated
    intent is checked against; an error reading them reports none (a write would be refused)."""
    if not active():
        return None
    try:
        folders = flagged_roots()
    except Exception as exc:
        logger.warning("the capability report lists no flagged folder: %s", type(exc).__name__)
        folders = []
    return {
        "status": STATUS,
        "write_scope": "marked_folders_only",
        "env_var": ENV_VAR,
        "marked_folders": list(folders),
        "gated_intents": sorted(_GATED),
        "always_refused_intents": sorted(_ALWAYS_REFUSED),
        "reason": (
            "On Windows, VOOL changes your files only inside folders marked in Settings, Folders VOOL "
            f"may change (or listed in {ENV_VAR}); a write anywhere else is refused before approval."
            + (f" {len(folders)} folder(s) are marked now." if folders else " No folder is marked now, so every such write is refused.")
        ),
    }


def _inside(path: str, roots: list[str], *, lexical: bool | None = None) -> bool:
    """Whether `path`, resolved, lies inside one of `roots`. Where the gate is on (`lexical`, by
    default `active()`), a spelling `_refused_spelling` refuses is never inside, and is not resolved:
    resolving a share is already the SMB connection."""
    # No file can have a path holding a NUL. POSIX realpath raises on one, but Windows realpath
    # returns it unresolved, so the text around the NUL would decide where it lies.
    if "\x00" in path:
        return False
    if (active() if lexical is None else lexical) and _refused_spelling(path):
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):  # a path the OS cannot resolve
        return False
    for root in roots:
        try:
            if os.path.commonpath([candidate, root]) == root:
                return True
        except ValueError:  # different drives
            continue
    return False


def inside_flagged(path: str, environ: dict[str, str] | None = None) -> bool:
    """Whether ``path``, resolved as the gate resolves it, lies inside a flagged folder. False when
    none is flagged. A lane asks this to pick a destination; the gate still decides every write."""
    try:
        roots = flagged_roots(environ)
    except Exception:  # an unknown state flags nothing
        return False
    return bool(roots) and _inside(str(path or ""), roots)


def _landing(intent: str, value: str) -> list[str] | None:
    """The places the writer of `intent` changes for the path `value`, or None when the gate cannot
    place it exactly.

    The gate must check the folders a call really changes, so it places each path from the
    argument exactly as that call's writer receives it, the way the writer does:
    - machine.write_file and machine.ensure_directory: stripped, from the user's home
      (`anchor_machine_path`);
    - machine.move_path: stripped, from the process's working directory (`_canonicalize`);
    - media.export: unstripped, from the working directory (`export_destination`), with the folder
      that holds the output, where the export also writes its stage files;
    - operator.move_path: stripped, with environment variables (`$VAR`, and `%VAR%` on Windows) and
      `~` expanded as its handler expands them. The handler takes a relative path from VOOL's
      working directory; the gate takes only a path that is absolute once expanded.
    Joined onto the chat workspace instead, `Documents\\x.txt` passed as a workspace file while the
    writer put it in the user's real Documents folder.
    """
    try:
        if intent in {"machine.write_file", "machine.ensure_directory"}:
            from core.runtime_execution_tools import anchor_machine_path

            return [str(anchor_machine_path(value))]
        if intent == "machine.move_path":
            from core.machine_file_ops import _canonicalize

            resolved = _canonicalize(value.strip())
            return None if resolved is None else [str(resolved)]
        if intent == "media.export":
            from core.media_studio_service import export_destination

            out = export_destination(value)
            return [str(out), str(out.parent)]
        if intent == "operator.move_path":
            expanded = Path(os.path.expandvars(value.strip())).expanduser()
            return [str(expanded)] if expanded.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def active(*, windows: bool | None = None) -> bool:
    """Whether this process enforces the Windows rule (patched by tests through `_IS_WINDOWS`)."""
    return bool(_IS_WINDOWS if windows is None else windows)


def _code_task_folder(task_id: Any) -> list[tuple[str, str]] | str:
    """The folder a code task writes in, found the way the task runtime finds the task.

    `CodeTaskRuntime._load` serves its cached copy while the journal's size and time are the ones it
    last read or wrote, even when the journal cannot be read now (another program holding it open);
    otherwise it takes the journal on disk, parsed by `CodeTask.from_json`, or nothing. An id it would
    not find gives no place here either: the call is then checked on the chat's workspace as before,
    and the runtime answers `unknown_task` without writing. A task it would load whose folder is not an
    absolute path cannot be placed and is refused. Never through `_load` itself, which refreshes the
    runtime's cache."""
    from core.code_assistant.task_runtime import _TASK_ID_RE, CodeTask, code_task_runtime, task_dir

    clean = str(task_id or "").strip()
    if not _TASK_ID_RE.match(clean):
        return []
    runtime = code_task_runtime()
    stat = runtime._journal_stat(clean)
    if stat is None:
        return []  # no journal: `_load` finds no task
    # Read without the runtime's lock, which a running step can hold. A pair caught mid-update either
    # fails the comparison (the journal is then read) or holds an older copy of the same task, and a
    # task's folder never changes once it is opened.
    task = runtime._tasks.get(clean) if runtime._stats.get(clean) == stat else None
    if task is None:
        try:
            task = CodeTask.from_json(json.loads((task_dir() / f"{clean}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):  # exactly what `_load` reads as no task
            return []
    folder = str(task.workspace_root or "").strip()
    if not _placeable(folder):
        return f"code task `{clean}` names no folder VOOL can place, so where it writes cannot be checked"
    return [("the code task's folder", folder)]


def _repo_session_folder(session_key: Any) -> str | None:
    """The folder a repo session runs in, read the way `RepoOpsRuntime._load` reads it: the journal
    on disk, or, while the journal is missing or unreadable, the runtime's own cached copy (which
    `_load` keeps serving). None when the runtime would not find the session either."""
    from core.repoops import plane

    key = str(session_key or "").strip()
    try:
        path = plane.session_journal_path(key)
    except ValueError:
        return None
    live = plane.repo_ops_runtime()._sessions.get(key)
    state, raw = plane._read_journal_bytes(path)
    session = live
    if state == "ok":
        try:
            parsed = plane.RepoSession.from_json(json.loads(raw.decode("utf-8")))
        except Exception:  # `_load` falls back to the cached copy on any parse failure
            parsed = live
        else:
            if parsed.session_key != key:
                return None
        session = parsed
    return None if session is None else str(getattr(session, "root", "") or "")


def _repo_session_places(intent: str, session_key: Any) -> list[tuple[str, str]] | str:
    folder = _repo_session_folder(session_key)
    if folder is None:
        return []  # the runtime answers unknown_session and runs nothing
    if not _placeable(folder):
        return "the repo session names no folder VOOL can place, so where it writes cannot be checked"
    places = [("the repo session's folder", folder)]
    if intent == "repo.git":
        git_places = _git_places(folder)
        if isinstance(git_places, str):
            return git_places
        places.extend(git_places)
    return places


def _git_places(folder: str) -> list[tuple[str, str]] | str:
    """Where git writes for a repository operation run in `folder`: the top of its work tree, its git
    folder and its shared git folder, as git itself reports them. The repository's own config can put
    any of them outside the folder (`core.worktree`, a `.git` file pointing elsewhere, a linked
    worktree), so they are asked of git, never derived from where `.git` sits. git is asked only when
    the operation's own runner would run git there (`GitRunner._repository_refusal`, which asks
    core/safe_git.py), and a refusal is given in that runner's words. A link below these three (in place
    of `.git/objects`), a hard link there, or a repository-set key that moves writes (Git LFS keys,
    `gc.repackFilterTo`) would send them elsewhere, so such a repository is refused
    (core/git_write_redirects.py). Each checked-out submodule's git folders are placed and checked the
    same way, since git and git-lfs also write there."""
    import subprocess

    from core import git_write_redirects, safe_git
    from core.repoops.gitops import GitRunner

    if safe_git.git_refused_here():  # no git process is started to place git's writes on Windows
        return safe_git.GIT_REFUSED_TEXT
    unplaceable = "git cannot say where this repository's work tree and git folders are, so where it writes cannot be checked"
    config_refusal = GitRunner(root=Path(folder))._repository_refusal(mutating=False)
    if config_refusal:
        return config_refusal
    try:
        probe = subprocess.run(
            safe_git.hardened_argv(Path(folder), "rev-parse", "--path-format=absolute", "--show-toplevel",
                                   "--git-dir", "--git-common-dir"),
            capture_output=True, encoding="utf-8", errors="surrogateescape", timeout=30, env=safe_git.safe_env(),
        )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return unplaceable
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) != 3 or not all(_placeable(line) for line in lines):
        return unplaceable
    try:
        repos = git_write_redirects.repositories(Path(folder), (lines[1], lines[2]))
        redirected = git_write_redirects.refusal_for(repos)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return "the repository's git folders or config could not be checked for links and settings that move its writes"
    if redirected:
        return f"{redirected}, so where it writes cannot be checked"
    places = list(zip(("the repository's work tree", "the repository's git folder", "the repository's shared git folder"),
                      lines, strict=True))
    for label, _, git_dir, common_dir in repos[1:]:  # each checked-out submodule, where git also writes
        places.extend(((f"{label}'s git folder", git_dir), (f"{label}'s shared git folder", common_dir)))
    return places


def _placeable(folder: str) -> bool:
    """An absolute folder the gate can place: on Windows it must name its drive or share, since a
    drive-less one ("/c/repo", as MSYS tools write it) would be read as a folder on the current drive."""
    return bool(folder) and os.path.isabs(folder) and (os.name != "nt" or bool(os.path.splitdrive(folder)[0]))


def _places_beyond_workspace(intent: str, arguments: dict[str, Any], step: tuple[str, dict[str, Any]] | None
                             ) -> list[tuple[str, str]] | str:
    """The folders a call changes besides the chat's workspace and its keyed paths, or a reason the
    gate cannot place them: the session's or task's folder of a step or rollback, and for a git
    operation the places git itself reports."""
    places: list[tuple[str, str]] = []
    if step is not None:
        step_intent, step_arguments = step
        found = (_repo_session_places("repo.step", step_arguments.get("repo_session_id"))
                 if step_intent == "repo.step" else _code_task_folder(step_arguments.get("task_id")))
        if isinstance(found, str):
            return found
        places.extend(found)
    if intent == "repo.git":
        found = _repo_session_places(intent, arguments.get("repo_session_id"))
    elif intent == "code.task.rollback":
        found = _code_task_folder(arguments.get("task_id"))
    else:
        found = []
    if isinstance(found, str):
        return found
    places.extend(found)
    return places


def may_refuse(intent: str, *, windows: bool | None = None) -> bool:
    """False when `refusal` is certain to allow this intent: off Windows, or an intent it never gates.

    Callers that ask the gate early (before the permission decision) check this first, so a call
    the gate has no say in gets exactly the handling it got before the early check existed: its
    arguments are not bound, its action is not built, and nothing about it can raise here.
    """
    if _git_refused(windows) and (_names_git_intent(intent) or intent in _COMMAND_INTENTS or intent in _STEPS):
        return True
    if not (_IS_WINDOWS if windows is None else windows):
        return False
    return intent in _GATED or intent in _STEPS


def _git_refused(windows: bool | None) -> bool:
    """Whether git intents are refused: on Windows (the gate's own test seam, `_IS_WINDOWS`), and
    wherever core/safe_git.py refuses git (any platform it cannot tell from Windows)."""
    if windows is not None:
        return bool(windows)
    from core import safe_git

    return bool(_IS_WINDOWS) or safe_git.git_refused_here()


def _names_git_intent(intent: str) -> bool:
    return intent in _GIT_INTENTS or intent.startswith(_GIT_INTENT_PREFIXES)


def _runs_git(intent: str, arguments: Any) -> bool:
    """Whether this call runs git: a git intent, or a command intent whose command starts git."""
    if _names_git_intent(intent):
        return True
    keys = _COMMAND_INTENTS.get(intent)
    if not keys or not isinstance(arguments, dict):
        return False
    from core import safe_git

    for key in keys:
        value = arguments.get(key)
        if value is None or value == "":
            continue
        try:
            if safe_git.command_text_runs_git(value):
                return True
        except Exception:
            return True  # a command that cannot be read is not let through to git (fail closed)
    return False


def refusal(
    intent: str,
    arguments: dict[str, Any],
    workspace_root: Path | str | None,
    *,
    windows: bool | None = None,
    environ: dict[str, str] | None = None,
    inner_intent_of: Callable[[dict[str, Any]], str] | None = None,
) -> str | None:
    """The reason this call must be refused on Windows, or None when it may proceed.

    Every place the call changes must lie inside a flagged folder: the chat's workspace, each keyed
    path where its writer puts it, and the folders the call really runs in, which are not the chat's
    for a step, a code-task rollback or a git operation (`_places_beyond_workspace`).

    A call that runs git (`_runs_git`, or a step whose inner call does) is refused first, whatever
    it names: VOOL runs no git on Windows in 0.7.1 (core/safe_git.py)."""
    if _git_refused(windows):
        from core import safe_git

        if _runs_git(intent, arguments):
            return safe_git.GIT_REFUSED_TEXT
        if intent in _STEPS:
            inner = str((inner_intent_of or (lambda args: str(args.get("intent") or "")))(arguments) or "").strip()
            if _runs_git(inner, arguments.get("arguments") or {}):
                return safe_git.GIT_REFUSED_TEXT
    if not (_IS_WINDOWS if windows is None else windows):
        return None
    step = None
    if intent in _STEPS:
        # Stripped as both step runtimes strip it before they classify and run the inner call.
        inner = str((inner_intent_of or (lambda args: str(args.get("intent") or "")))(arguments) or "").strip()
        if inner not in _GATED:
            return None
        inner_arguments = arguments.get("arguments") or {}
        if not isinstance(inner_arguments, dict):
            return f"{intent} carries step arguments that are not named values, so its target cannot be checked; nothing was changed."
        step = (intent, arguments)
        intent, arguments = inner, dict(inner_arguments)
    if intent not in _GATED:
        return None
    if intent in _ALWAYS_REFUSED:
        return (
            "On Windows, VOOL does not clean temp files until its Windows filesystem protection is "
            "complete: a cleanup also empties the system temp folders and a plan stored earlier, which "
            f"are not limited to the folders marked in {ENV_VAR}. Nothing was changed."
        )
    # Every path the call names, judged by its spelling before anything places, resolves or stats it:
    # `_landing` resolves a machine path, and `_inside` resolves every target.
    if workspace_root and intent not in _LANDING_ONLY:
        kind = _refused_spelling(workspace_root)
        if kind:
            return spelling_refusal_text(workspace_root, kind, what="folder this call works in")
    for key in _GATED[intent]:
        kind = _refused_spelling(arguments.get(key))
        if kind:
            return spelling_refusal_text(arguments.get(key), kind, what=key)
    roots = flagged_roots(environ)
    targets = [str(workspace_root)] if workspace_root and intent not in _LANDING_ONLY else []
    for key in _GATED[intent]:
        value = str(arguments.get(key) or "")
        if not value.strip():
            return f"{intent} names no {key}, so its target cannot be checked"
        landing = _landing(intent, value)
        if landing is None:
            if intent.startswith("operator."):
                return (f"{intent} needs a {key} that is absolute once ~ and environment variables are expanded, "
                        "so its target cannot be checked")
            return f"{intent} names a {key} the gate cannot place exactly, so its target cannot be checked"
        targets.extend(landing)
    if roots and targets and all(_inside(target, roots, lexical=True) for target in targets):
        # Only once every place the arguments name is inside: the folders the call runs in, which
        # need a journal read (and for git, one git process).
        try:
            places = _places_beyond_workspace(intent, arguments, step)
        except Exception as exc:  # an unknown state fails closed, never as a crash of the turn
            places = f"where this call writes could not be checked ({type(exc).__name__})"
        if isinstance(places, str):
            return f"{places}; nothing was changed."
        outside = [(label, place) for label, place in places if not _inside(place, roots, lexical=True)]
        if not outside:
            return None
        label, place = outside[0]
        return (
            f"On Windows, VOOL changes files only inside folders explicitly marked as disposable "
            f"development workspaces ({ENV_VAR}) until its Windows filesystem protection is complete. "
            f"This call also changes {label} ({place}), which is outside the marked folders; nothing was changed."
        )
    return (
        f"On Windows, VOOL changes files only inside folders explicitly marked as disposable "
        f"development workspaces ({ENV_VAR}) until its Windows filesystem protection is complete. "
        f"{'No folder is marked' if not roots else 'This target is outside the marked folders'}; nothing was changed."
    )


__all__ = ["DOUBLED_DRIVE", "ENV_VAR", "REMOTE_DRIVE", "STATUS", "active", "broker_report", "flagged_roots",
           "inside_flagged", "may_refuse", "refusal", "refused_spelling", "spelling_refusal_text", "wide_root_reason"]
