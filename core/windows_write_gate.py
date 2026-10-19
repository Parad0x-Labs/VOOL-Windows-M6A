"""Windows: user-filesystem writes run only inside explicitly flagged disposable dev workspaces.

Until the handle-based Windows filesystem authority exists, a reviewed write on Windows is
checked by path and then performed by path, and that window can be raced, so it is not
confinement. Owner decision (2026-10-18): on Windows, intents that change the user's files are
refused unless every target lies inside a folder listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES
(os.pathsep-separated absolute paths), each path argument checked where its writer puts it
(`_landing`); an intent that also changes folders its arguments do not name is refused outright
(`_ALWAYS_REFUSED`). The developer sets that variable when starting VOOL; nothing inside a
workspace can set it. VOOL's own private stores (skills, media projects, browser profiles,
notes) are not user-filesystem destinations and are not gated here. POSIX is unaffected.
"""
from __future__ import annotations

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
    try:
        candidate = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):  # a path no file can have, such as one holding a NUL
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
    - operator.move_path: stripped, with `$VAR` and `~` expanded as its handler expands them. The
      handler takes a relative path from VOOL's working directory; the gate takes only a path
      that is absolute once expanded.
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
    """The reason this call must be refused on Windows, or None when it may proceed."""
    if not (_IS_WINDOWS if windows is None else windows):
        return None
    if intent in _STEPS:
        inner = (inner_intent_of or (lambda args: str(args.get("intent") or "")))(arguments)
        if inner not in _GATED:
            return None
        return refusal(inner, dict(arguments.get("arguments") or {}), workspace_root,
                       windows=True, environ=environ)
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
                return f"{intent} needs a {key} that is absolute once ~ and $VAR are expanded, so its target cannot be checked"
            return f"{intent} names a {key} the gate cannot place exactly, so its target cannot be checked"
        targets.extend(landing)
    if roots and targets and all(_inside(target, roots) for target in targets):
        return None
    return (
        f"On Windows, VOOL changes files only inside folders explicitly marked as disposable "
        f"development workspaces ({ENV_VAR}) until its Windows filesystem protection is complete. "
        f"{'No folder is marked' if not roots else 'This target is outside the marked folders'}; nothing was changed."
    )


__all__ = ["ENV_VAR", "STATUS", "flagged_roots", "may_refuse", "refusal"]
