"""Windows: an edit, an undo of it, or a move of a file the owner names inside a marked folder.

Captest round 1, R1-4 (build 828b8bda, clean Win11 VM, 2026-11-09). Creating a file in a marked
folder worked (the `machine.write_file` lane, core/agent_runtime/fast_paths_machine.py). Editing a
file there, "undo the last change", moving a file between two marked folders and editing a file with
mixed CRLF/LF line endings all acted on the chat's own workspace instead, which for a new chat is
the Desktop: "No text matches", "There is no file at `mixed.txt`", "outside the active workspace
(Desktop)". The workspace tools resolve every path inside the chat's workspace, and no lane read a
full path to a file in a marked folder for an edit or a move.

This lane reads those three asks deterministically and runs each through the tool that already
owns it, for that one call only:

- an edit (``replace 'a' with 'b' in <file>``, or ``change 'a' to 'b'``) runs
  `workspace.replace_in_file` placed in the named file's own folder. That writer keeps every byte
  around the replaced text (CRLF, LF, trailing spaces) and records the change in the chat's change
  ledger with the exact earlier text;
- an undo (``undo the last change``) runs `workspace.rollback_last_change` in the folder of the
  newest change this chat recorded, when that folder is still marked. It restores the exact earlier
  bytes, or refuses when the file changed since; with no recorded change it says so and changes
  nothing;
- a move (``move <file> to <folder>``) runs `machine.move_path` with both paths resolved, which
  keeps its own live OS confirmation.

The chat's workspace, the model's tools and every other lane are left as they were: nothing here
rebinds the turn. Each call goes through `execute_authorized_runtime_tool`, so approval is asked
exactly as for any other write, and the Windows write gate checks the call again where it lands.

A path is acted on only when all of these hold, judged before anything else looks at it:
- the turn is the owner's own local turn;
- it is a full local path: a share, a device path or a mapped network drive is never looked up
  (on Windows a drive letter is looked at only when GetDriveTypeW calls it local; an unknown drive
  counts as remote);
- where it really leads, links and junctions followed, is inside a folder marked now
  (`windows_write_gate.inside_flagged`);
- the named file itself is not a link.
Anything else is refused in plain words, and nothing is changed. Off Windows nothing changes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from core import windows_write_gate

#: Patched by tests to drive the drive-letter rule on any platform.
_ON_WINDOWS = os.name == "nt"

_NOTHING_CHANGED = "Nothing was changed."
_NOT_MARKED = (
    "`{path}` is not inside a folder you marked in Settings, Folders VOOL may change (where it really "
    "leads, links and junctions followed, is what counts), so I can't change it. "
)
_NOT_LOCAL = (
    "`{path}` is not a full path on a local drive (a share, a device path or a mapped network drive), "
    "so I don't look it up. "
)


@dataclass(frozen=True)
class MarkedFileAsk:
    """What the lane does with a turn: one tool call placed in ``workspace``, or a plain refusal."""

    kind: str  # "edit", "undo" or "move"
    intent: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    refusal: str = ""


def _value(name: str, *, bare: bool) -> str:
    from core.execution.planner import _quoted_value_pattern

    return "(?:" + _quoted_value_pattern(name) + (rf"|(?P<{name}bare>[A-Za-z0-9_.:-]+)" if bare else "") + ")"


_WORD = r"(?:the\s+(?:word|text)\s+)?"
#: "replace a with b" may name single words bare, as the planner reads it; "change ... to ..." only
#: in quotes, since "change the font to bold" names no text to replace.
_EDIT_RES = (
    re.compile(r"\breplace\s+" + _WORD + _value("old", bare=True) + r"\s+with\s+" + _WORD + _value("new", bare=True),
               re.IGNORECASE),
    re.compile(r"\bchange\s+" + _WORD + _value("old", bare=False) + r"\s+(?:to|into)\s+" + _WORD
               + _value("new", bare=False), re.IGNORECASE),
)
_UNDO_RE = re.compile(
    r"^\s*(?:please\s+|ok(?:ay)?,?\s+|now\s+)?(?:undo|revert|roll\s*back)"
    r"(?:\s+(?:that|this|it|the\s+(?:last\s+)?(?:change|edit)|my\s+last\s+(?:change|edit)|that\s+(?:change|edit)"
    r"|the\s+last\s+thing(?:\s+you\s+did)?|what\s+you\s+(?:just\s+)?(?:did|changed)))?"
    r"(?:,?\s+please)?\s*[.!]?\s*$",
    re.IGNORECASE,
)
_MOVE_VERB_RE = re.compile(r"\b(?:move|mv)(?:\s+(?:the\s+)?file)?\s*\Z", re.IGNORECASE)
_MOVE_LINK_RE = re.compile(r"(?:to|into|in|over\s+to)(?:\s+the)?(?:\s+folder)?", re.IGNORECASE)
#: A bare file name an edit names next to a folder: "... in mixed.txt in <folder>".
#: A name with spaces is read only in quotes; a quoted name holds no folder separator.
_BARE_NAME_RE = re.compile(
    r"(?P<quote>[\"'`])(?P<qname>[^\"'`\\/<>|:*?\r\n]+\.[A-Za-z0-9_+-]{1,12})(?P=quote)"
    r"|(?<![\w\\/.-])(?P<name>[^\s\\/'\"`<>|:*?]+\.[A-Za-z0-9_+-]{1,12})(?![\w\\/-])"
)


def _quoted(match: re.Match[str], name: str) -> str:
    from core.execution.planner import _quoted_group

    value = _quoted_group(match, name)
    return value if value else str(match.groupdict().get(f"{name}bare") or "")


def _owner_local(source_context: dict[str, Any] | None) -> bool:
    from core.request_trust import OWNER_LOCAL_KEY

    return bool((source_context or {}).get(OWNER_LOCAL_KEY) is True)


def _candidates(body: str) -> list[tuple[int, str]]:
    from core.agent_runtime import write_root_honesty as paths

    return list(paths._destination_candidates(body))


def _local(body: str, start: int, path: str) -> bool:
    """Whether ``path`` may be looked up at all: a full path on a local drive. Judged on its spelling
    and, for a drive letter on Windows, on GetDriveTypeW, which does no I/O on a share."""
    from core import windows_known_folders
    from core.agent_runtime import write_root_honesty as paths

    if paths.typed_path_kind(body, start, path):
        return False
    drive = re.match(r"^[A-Za-z]:(?=[\\/])", path)
    if _ON_WINDOWS:
        return bool(drive) and windows_known_folders._drive_is_remote(drive.group(0)) is False
    return os.path.isabs(path) and not drive


def _is_link(path: str) -> bool:
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def _real_folder(path: str) -> str:
    """Where the folder ``path`` really is, links followed; ``""`` when it cannot be placed."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return ""


def _marked_file(path: str) -> tuple[str, str, str]:
    """``(folder, name, refusal)`` for a file named by its full path: its real folder and its name,
    or why it cannot be acted on."""
    if "\x00" in path:
        return "", "", _NOT_MARKED.format(path=path)
    try:
        exists, is_file = os.path.lexists(path), os.path.isfile(path)
    except (OSError, ValueError):
        exists, is_file = False, False
    if not exists:
        return "", "", f"There is no file at `{path}`. "
    if _is_link(path):
        return "", "", f"`{path}` is a link, and I change only the file itself, never what a link leads to. "
    if not is_file:
        return "", "", f"`{path}` is a folder, not a file. "
    folder = _real_folder(os.path.dirname(path))
    if not folder or not windows_write_gate.inside_flagged(folder) or not windows_write_gate.inside_flagged(
        os.path.join(folder, os.path.basename(path))
    ):
        return "", "", _NOT_MARKED.format(path=path)
    return folder, os.path.basename(path), ""


def _blank(body: str, start: int, end: int) -> str:
    return body[:start] + " " * (end - start) + body[end:]


def _edit(text: str) -> MarkedFileAsk | None:
    match = next((found for found in (pattern.search(text) for pattern in _EDIT_RES) if found), None)
    if match is None:
        return None
    old, new = _quoted(match, "old"), _quoted(match, "new")
    body = _blank(text, match.start(), match.end())
    candidates = _candidates(body)
    if not candidates:
        return None  # no full path: the chat's own workspace lanes read it, as before
    refusal = _first_unlookable(body, candidates)
    if refusal:
        return MarkedFileAsk("edit", refusal=refusal + _NOTHING_CHANGED)
    if len({path for _start, path in candidates}) != 1:
        return MarkedFileAsk("edit", refusal="Name one file to change, by its full path. " + _NOTHING_CHANGED)
    start, named = candidates[0]
    target = named
    try:
        named_is_dir = os.path.isdir(named) and not _is_link(named)
    except (OSError, ValueError):
        named_is_dir = False
    if named_is_dir:
        rest = _blank(body, start, start + len(named))
        names = {found.group("qname") or found.group("name") for found in _BARE_NAME_RE.finditer(rest)}
        if len(names) != 1:
            return MarkedFileAsk(
                "edit", refusal=f"`{named}` is a folder; name the file in it to change. " + _NOTHING_CHANGED
            )
        target = os.path.join(named, names.pop())
    if not old:
        return MarkedFileAsk("edit", refusal="Say which text to replace. " + _NOTHING_CHANGED)
    folder, name, refusal = _marked_file(target)
    if refusal:
        return MarkedFileAsk("edit", refusal=refusal + _NOTHING_CHANGED)
    return MarkedFileAsk(
        "edit", intent="workspace.replace_in_file", workspace=folder,
        arguments={"path": name, "old_text": old, "new_text": new},
    )


def _first_unlookable(body: str, candidates: list[tuple[int, str]]) -> str:
    for start, path in candidates:
        if not _local(body, start, path):
            return _NOT_LOCAL.format(path=path)
    return ""


def _move(text: str) -> MarkedFileAsk | None:
    candidates = _candidates(text)
    if not candidates or not _MOVE_VERB_RE.search(text[:candidates[0][0]].rstrip("\"'`")):
        return None
    # A share or a drive Windows does not call local is refused before it is read any further.
    refusal = _first_unlookable(text, candidates)
    if refusal:
        return MarkedFileAsk("move", refusal=refusal + "Nothing was moved.")
    if len(candidates) != 2:
        return None
    (src_start, source), (dst_start, destination) = candidates
    between = text[src_start + len(source):dst_start].strip(" \t\"'`.,;:!?)")
    if not _MOVE_LINK_RE.fullmatch(between):
        return None
    folder, name, refusal = _marked_file(source)
    if refusal:
        return MarkedFileAsk("move", refusal=refusal + "Nothing was moved.")
    try:
        into_folder = os.path.isdir(destination)
        parent = destination if into_folder else os.path.dirname(destination)
        parent_exists = os.path.isdir(parent)
    except (OSError, ValueError):
        into_folder, parent, parent_exists = False, "", False
    if not parent_exists:
        return MarkedFileAsk("move", refusal=f"There is no folder at `{parent or destination}`. Nothing was moved.")
    real_parent = _real_folder(parent)
    final = os.path.join(real_parent, name if into_folder else os.path.basename(destination))
    if not real_parent or not windows_write_gate.inside_flagged(real_parent) or not windows_write_gate.inside_flagged(final):
        return MarkedFileAsk(
            "move",
            refusal=(
                f"`{destination}` is outside the folders you marked in Settings, Folders VOOL may change "
                "(where it really leads, links and junctions followed, is what counts), so I won't move "
                f"`{source}` there. Nothing was moved."
            ),
        )
    if os.path.lexists(final):
        return MarkedFileAsk("move", refusal=f"`{final}` already exists. Nothing was moved.")
    return MarkedFileAsk(
        "move", intent="machine.move_path", workspace=folder,
        arguments={"source": os.path.join(folder, name), "destination": final},
    )


def _undo(text: str, session_id: str) -> MarkedFileAsk | None:
    if not _UNDO_RE.match(text):
        return None
    from core.execution.artifacts import latest_workspace_mutation_for_session

    none_left = (
        "There is no change VOOL made in a marked folder in this chat that is left to undo. "
        + _NOTHING_CHANGED
    )
    if not session_id:
        return MarkedFileAsk("undo", refusal=none_left)
    try:
        record = latest_workspace_mutation_for_session(session_id)
    except Exception:  # an unreadable ledger restores nothing on a guess
        return MarkedFileAsk("undo", refusal="I could not read this chat's change record. " + _NOTHING_CHANGED)
    if record is None:
        return MarkedFileAsk("undo", refusal=none_left)
    folder = str(record.get("workspace_root") or "").strip()
    if (not folder or not os.path.isabs(folder) or _is_link(folder)
            or os.path.normcase(_real_folder(folder)) != os.path.normcase(folder)
            or not windows_write_gate.inside_flagged(folder)):
        return MarkedFileAsk(
            "undo",
            refusal=(
                f"The last change VOOL made in this chat was in `{folder}`, which is not a folder you have "
                "marked now, so I won't undo it there. " + _NOTHING_CHANGED
            ),
        )
    return MarkedFileAsk("undo", intent="workspace.rollback_last_change", workspace=folder, arguments={})


def marked_file_ask(text: str, *, source_context: dict[str, Any] | None, session_id: str = "") -> MarkedFileAsk | None:
    """What this turn asks of a file in a marked folder, or None for a turn this lane leaves alone:
    off Windows, a turn that is not the owner's own local one, and any other ask."""
    if not windows_write_gate.active() or not _owner_local(source_context):
        return None
    body = str(text or "")
    if not body.strip():
        return None
    ledger_session = str((source_context or {}).get("session_id") or "").strip() or str(session_id or "").strip()
    for reader in (lambda: _undo(body, ledger_session), lambda: _move(body), lambda: _edit(body)):
        ask = reader()
        if ask is not None:
            return ask
    return None


def placed_context(ask: MarkedFileAsk, source_context: dict[str, Any] | None) -> dict[str, Any]:
    """The source context for the ask's one call: the chat's own, with its workspace set to the folder
    the call acts in."""
    context = dict(source_context or {})
    context["workspace"] = ask.workspace
    context["workspace_root"] = ask.workspace
    return context


__all__ = ["MarkedFileAsk", "marked_file_ask", "placed_context"]
