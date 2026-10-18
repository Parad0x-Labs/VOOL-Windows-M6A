"""Windows: user-filesystem writes run only inside explicitly flagged disposable dev workspaces.

Until the handle-based Windows filesystem authority exists, a reviewed write on Windows is
checked by path and then performed by path, and that window can be raced, so it is not
confinement. Owner decision (2026-10-18): on Windows, intents that change the user's files are
refused unless every target lies inside a folder listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES
(os.pathsep-separated absolute paths). The developer sets that variable when starting VOOL;
nothing inside a workspace can set it. VOOL's own private stores (skills, media projects,
browser profiles, notes) are not user-filesystem destinations and are not gated here. POSIX is
unaffected.
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
#: An empty tuple means the target is the workspace itself.
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


def flagged_roots(environ: dict[str, str] | None = None) -> list[str]:
    raw = (os.environ if environ is None else environ).get(ENV_VAR, "")
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part and os.path.isabs(part) and os.path.isdir(part):
            roots.append(os.path.normcase(os.path.realpath(part)))
    return roots


def _inside(path: str, roots: list[str]) -> bool:
    candidate = os.path.normcase(os.path.realpath(path))
    for root in roots:
        try:
            if os.path.commonpath([candidate, root]) == root:
                return True
        except ValueError:  # different drives
            continue
    return False


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
    roots = flagged_roots(environ)
    targets = [str(workspace_root)] if workspace_root else []
    for key in _GATED[intent]:
        value = str(arguments.get(key) or "").strip()
        if not value:
            return f"{intent} names no {key}, so its target cannot be checked"
        if not os.path.isabs(value) and not workspace_root:
            return f"{intent} names a relative {key} with no workspace to anchor it"
        targets.append(value if os.path.isabs(value) else os.path.join(str(workspace_root), value))
    if roots and targets and all(_inside(target, roots) for target in targets):
        return None
    return (
        f"On Windows, VOOL changes files only inside folders explicitly marked as disposable "
        f"development workspaces ({ENV_VAR}) until its Windows filesystem protection is complete. "
        f"{'No folder is marked' if not roots else 'This target is outside the marked folders'}; nothing was changed."
    )


__all__ = ["ENV_VAR", "STATUS", "flagged_roots", "may_refuse", "refusal"]
