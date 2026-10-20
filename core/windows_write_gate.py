"""Windows: user-filesystem writes run only inside explicitly flagged disposable dev workspaces.

Until the handle-based Windows filesystem authority exists, a reviewed write on Windows is
checked by path and then performed by path, and that window can be raced, so it is not
confinement. Owner decision (2026-10-18): on Windows, intents that change the user's files are
refused unless every target lies inside a folder listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES
(os.pathsep-separated absolute paths), each path argument checked where its writer puts it
(`_landing`), and every folder the call really runs in checked too: a step's or a code-task rollback's
task or session folder, and for a git operation the top of the repository's work tree, its git folder and
its shared git folder, as git reports them (`_places_beyond_workspace`). Only those folders are placed: a
link or junction inside the git folder, or a repository-local `lfs.storage` setting, can still send some
of git's writes elsewhere. An intent that also changes folders its arguments do not name is refused
outright (`_ALWAYS_REFUSED`).
The developer sets that variable when starting VOOL; nothing inside a
workspace can set it. VOOL's own private stores (skills, media projects, browser profiles,
notes) are not user-filesystem destinations and are not gated here. POSIX is unaffected.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
#: Step intents whose effect is the inner intent's (gated only when that inner intent is gated).
_STEPS = frozenset({"code.task.step", "repo.step"})
#: Gated intents refused on Windows whatever they name, because they also change folders their
#: arguments do not name: a temp cleanup empties the folders TMPDIR, TMP and TEMP name, and its
#: approval empties the plan a disk scan stored earlier (core.operator.handlers).
_ALWAYS_REFUSED = frozenset({"operator.cleanup_temp_files"})


def flagged_roots(environ: dict[str, str] | None = None) -> list[str]:
    raw = (os.environ if environ is None else environ).get(ENV_VAR, "")
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part and os.path.isabs(part) and os.path.isdir(part):
            roots.append(os.path.normcase(os.path.realpath(part)))
    return roots


def _inside(path: str, roots: list[str]) -> bool:
    # No file can have a path holding a NUL. POSIX realpath raises on one, but Windows realpath
    # returns it unresolved, so the text around the NUL would decide where it lies.
    if "\x00" in path:
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
    core/safe_git.py), and a refusal is given in that runner's words. Folders below these three (a link
    in place of `.git/objects`) and a local `lfs.storage` setting are not placed."""
    import subprocess

    from core import safe_git
    from core.repoops.gitops import GitRunner

    unplaceable = "git cannot say where this repository's work tree and git folders are, so where it writes cannot be checked"
    config_refusal = GitRunner(root=Path(folder))._repository_refusal(mutating=False)
    if config_refusal:
        return config_refusal
    try:
        probe = subprocess.run(
            safe_git.hardened_argv(Path(folder), "rev-parse", "--path-format=absolute", "--show-toplevel",
                                   "--git-dir", "--git-common-dir"),
            capture_output=True, text=True, timeout=30, env=safe_git.safe_env(),
        )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return unplaceable
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) != 3 or not all(_placeable(line) for line in lines):
        return unplaceable
    return list(zip(("the repository's work tree", "the repository's git folder", "the repository's shared git folder"),
                    lines, strict=True))


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
    if not (_IS_WINDOWS if windows is None else windows):
        return False
    return intent in _GATED or intent in _STEPS


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
    for a step, a code-task rollback or a git operation (`_places_beyond_workspace`)."""
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
    roots = flagged_roots(environ)
    targets = [str(workspace_root)] if workspace_root else []
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
    if roots and targets and all(_inside(target, roots) for target in targets):
        # Only once every place the arguments name is inside: the folders the call runs in, which
        # need a journal read (and for git, one git process).
        try:
            places = _places_beyond_workspace(intent, arguments, step)
        except Exception as exc:  # an unknown state fails closed, never as a crash of the turn
            places = f"where this call writes could not be checked ({type(exc).__name__})"
        if isinstance(places, str):
            return f"{places}; nothing was changed."
        outside = [(label, place) for label, place in places if not _inside(place, roots)]
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


__all__ = ["ENV_VAR", "STATUS", "active", "flagged_roots", "may_refuse", "refusal"]
