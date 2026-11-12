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

    kind: str  # "edit", "write", "undo", "move" or "patch"
    intent: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    refusal: str = ""
    #: For an undo that moves a file back: the ledger record of the move it undoes.
    undoes: str = ""


def _value(name: str, *, bare: bool) -> str:
    from core.execution.planner import _quoted_value_pattern

    return "(?:" + _quoted_value_pattern(name) + (rf"|(?P<{name}bare>[A-Za-z0-9_.:-]+)" if bare else "") + ")"


_WORD = r"(?:the\s+(?:word|text)\s+)?"
#: How a request may open before its verb. Every reader here is anchored at the start of a one-line
#: message (as `_UNDO_RE` and the program-request lane are), so a pasted log or a sentence that only
#: mentions "replace 'a' with 'b' in <file>" is never read as a request: in Auto mode it would run
#: with no approval (Security Architect review of R1-4, 9 Nov 2026).
_LEAD = r"^\s*(?:(?:please|ok(?:ay)?|now)\b[,]?\s+)*"
#: "replace a with b" may name single words bare, as the planner reads it; "change ... to ..." only
#: in quotes, since "change the font to bold" names no text to replace.
_EDIT_RES = (
    re.compile(r"replace\s+" + _WORD + _value("old", bare=True) + r"\s+with\s+" + _WORD + _value("new", bare=True),
               re.IGNORECASE),
    re.compile(r"change\s+" + _WORD + _value("old", bare=False) + r"\s+(?:to|into)\s+" + _WORD
               + _value("new", bare=False), re.IGNORECASE),
)
_UNDO_RE = re.compile(
    r"^\s*(?:please\s+|ok(?:ay)?,?\s+|now\s+)?(?:undo|revert|roll\s*back)"
    r"(?:\s+(?:that|this|it|the\s+(?:last\s+)?(?:change|edit)|my\s+last\s+(?:change|edit)|that\s+(?:change|edit)"
    r"|the\s+last\s+thing(?:\s+you\s+did)?|what\s+you\s+(?:just\s+)?(?:did|changed)))?"
    r"(?:,?\s+please)?\s*[.!]?\s*$",
    re.IGNORECASE,
)
_LEAD_RE = re.compile(_LEAD, re.IGNORECASE)
#: The one other opening an edit may have: the folder first ("in <folder>, replace 'a' with 'b' in x.txt").
_FOLDER_FIRST_RE = re.compile(r"in\s+[\"'`]?", re.IGNORECASE)
_MOVE_VERB_RE = re.compile(_LEAD + r"(?:move|mv)(?:\s+(?:the\s+)?file)?\s*\Z", re.IGNORECASE)
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

    from core import windows_path_policy

    if paths.typed_path_kind(body, start, path) or windows_path_policy.refused_doubled_drive(path):
        return False  # `typed_path_kind` asks the doubled-separator rule too; asked here as well
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


def _edit_request_start(text: str) -> int:
    """Where the verb of an edit request may stand: after the lead words, or after a leading
    "in <full path>," that names the folder first; -1 when the message opens any other way."""
    lead = _LEAD_RE.match(text)
    pos = lead.end() if lead else 0
    if any(pattern.match(text, pos) for pattern in _EDIT_RES):
        return pos
    folder = _FOLDER_FIRST_RE.match(text, pos)
    if folder is None:
        return -1
    for start, path in _candidates(text):
        if start == folder.end():
            after = re.compile(r"[\"'`]?\s*,?\s*").match(text, start + len(path))
            return after.end() if after else -1
    return -1


def _edit(text: str) -> MarkedFileAsk | None:
    pos = _edit_request_start(text)
    if pos < 0:
        return None
    match = next((found for found in (pattern.match(text, pos) for pattern in _EDIT_RES) if found), None)
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


#: An overwrite or an append of a file the owner names by its full path (captest b58c5eb0, M3). The text
#: is quoted, or one bare word; after "with" at the end of the message it may be bare to the end.
_TEXT_VALUE = r"(?:[\"'`\u201c\u2018](?P<q>[^\"'`\u201c\u201d\u2018\u2019\r\n]*)[\"'`\u201d\u2019]|(?P<bare>[^\s\"'`]+))"
_OPEN_QUOTE = r"\s*[\"'`]?\s*\Z"
_WRITE_BEFORE_RES = (
    # write the text X to <file>
    ("overwrite", re.compile(_LEAD + r"(?:write|put)\s+(?:the\s+)?text\s+" + _TEXT_VALUE
                             + r"\s+(?:to|in|into)(?:\s+the\s+file)?" + _OPEN_QUOTE, re.IGNORECASE)),
    # append the line X to <file>
    ("append", re.compile(_LEAD + r"(?:append|add)\s+(?:the\s+)?line\s+" + _TEXT_VALUE
                          + r"\s+(?:to|at\s+the\s+end\s+of)(?:\s+the\s+file)?" + _OPEN_QUOTE, re.IGNORECASE)),
)
#: replace the contents of <file> with (the text) X
_REPLACE_ALL_BEFORE_RE = re.compile(
    _LEAD + r"replace\s+(?:all\s+)?(?:the\s+)?(?:contents?|text)\s+(?:of|in)(?:\s+the\s+file)?" + _OPEN_QUOTE,
    re.IGNORECASE,
)
#: overwrite (the file | the contents of) <file> with (the text) X (hardware captest, 0.7.1: this went to the
#: model, which answered "Let me do that now." and wrote nothing). Read as "replace the contents of".
_OVERWRITE_BEFORE_RE = re.compile(
    _LEAD + r"overwrite(?:\s+(?:the\s+)?(?:file|contents?\s+of(?:\s+the\s+file)?))?" + _OPEN_QUOTE,
    re.IGNORECASE,
)
_REPLACE_ALL_AFTER_RE = re.compile(
    r"\A[\"'`]?\s*,?\s+with\s+(?:the\s+)?(?:text\s+)?(?:[\"'`\u201c\u2018](?P<q>[^\"'`\u201c\u201d\u2018\u2019\r\n]*)"
    r"[\"'`\u201d\u2019]|(?P<bare>[^\"'`\r\n]+?))\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
_PATH_TAIL_RE = re.compile(r"\A[\"'`]?\s*[.!]?\s*\Z")


def _text_of(match: re.Match[str]) -> str:
    quoted = match.group("q")
    return quoted if quoted is not None else str(match.group("bare") or "")


def _write(text: str) -> MarkedFileAsk | None:
    """An overwrite ("write the text X to <file>", "replace the contents of <file> with X") or an append
    ("append the line X to <file>") of a file that exists in a marked folder: one `workspace.write_file`
    placed in the file's own folder, with the hash of the bytes read here, so it is journaled (and
    "undo that" restores them) and refused if the file changed before it runs. A file that is not there
    yet is a create, left to the machine.write_file lane."""
    from core.agent_runtime import write_root_honesty as paths

    candidates = list(paths._destination_candidates(text, typed_shares=True))
    if not candidates:
        return None
    start, named = candidates[0]
    before, after = text[:start], text[start + len(named):]
    kind, value, tail = "", None, None
    for found_kind, pattern in _WRITE_BEFORE_RES:
        found = pattern.match(before)
        if found:
            kind, value, tail = found_kind, _text_of(found), _PATH_TAIL_RE.match(after)
            break
    replaces = False
    if not kind and (_REPLACE_ALL_BEFORE_RE.match(before) or _OVERWRITE_BEFORE_RE.match(before)):
        kind, replaces = "overwrite", True
        tail = _REPLACE_ALL_AFTER_RE.match(after)
        value = _text_of(tail) if tail else None
    if not kind:
        return None
    # A share or a drive Windows does not call local is refused before it is read any further: where
    # such a path ends is not looked up, so the rest of the message is not read against it.
    refusal = _first_unlookable(text, candidates)
    if refusal:
        return MarkedFileAsk("write", refusal=refusal + _NOTHING_CHANGED)
    if tail is None or value is None:
        return None
    if len({path for _start, path in candidates}) != 1:
        return MarkedFileAsk("write", refusal="Name one file to change, by its full path. " + _NOTHING_CHANGED)
    if ".." in re.split(r"[\\/]", named):
        return MarkedFileAsk(
            "write", refusal=f"`{named}` climbs out of a folder with `..`; name the file by its own full path. "
            + _NOTHING_CHANGED,
        )
    try:
        present = os.path.lexists(named)
    except (OSError, ValueError):
        present = False
    if not present:
        if not replaces:
            return None
        # "replace the contents of" or "overwrite" a file that is not there: said plainly here rather than
        # left to the model, which promised the write and made none (hardware captest, 0.7.1).
        parent = _real_folder(os.path.dirname(named))
        if not parent or not windows_write_gate.inside_flagged(parent) or not windows_write_gate.inside_flagged(
            os.path.join(parent, os.path.basename(named))
        ):
            return MarkedFileAsk("write", refusal=_NOT_MARKED.format(path=named) + _NOTHING_CHANGED)
        return MarkedFileAsk(
            "write",
            refusal=f"There is no file at `{named}` to replace. To make it, say \"write the text {value} to {named}\". "
            + _NOTHING_CHANGED,
        )
    folder, name, refusal = _marked_file(named)
    if refusal:
        return MarkedFileAsk("write", refusal=refusal + _NOTHING_CHANGED)
    try:
        with open(os.path.join(folder, name), "rb") as handle:
            current = handle.read()
    except OSError:
        return MarkedFileAsk("write", refusal=f"I could not read `{named}`. " + _NOTHING_CHANGED)
    if kind == "append":
        try:
            old_text = current.decode("utf-8")
        except UnicodeDecodeError:
            return MarkedFileAsk(
                "write", refusal=f"`{named}` is not plain UTF-8 text, so I won't add a line to it. " + _NOTHING_CHANGED
            )
        newline = "\r\n" if "\r\n" in old_text else "\n"
        ends_open = not old_text or old_text.endswith(("\n", "\r"))
        content = old_text + ("" if ends_open else newline) + value + (newline if old_text and ends_open else "")
    else:
        content = value
    import hashlib

    return MarkedFileAsk(
        "write", intent="workspace.write_file", workspace=folder,
        arguments={"path": name, "content": content, "expected_hash": hashlib.sha256(current).hexdigest()},
    )


#: The first line of a patch request: "apply this patch", "apply the following unified diff to calc.py:",
#: "patch calc.py with this:" (claims sweep, Windows). Anchored at the message's start, as every reader
#: here is, so a pasted diff with no request before it is never read as one.
_PATCH_VERB_RE = re.compile(
    r"(?:apply|use)\b[^\n]{0,80}?\b(?:patch|diff)(?:es)?\b[^\n]*\Z|patch\b[^\n]*\Z", re.IGNORECASE
)
#: A diff in a fenced block, tagged diff, patch, udiff or nothing.
_FENCED_PATCH_RE = re.compile(r"```(?:diff|patch|udiff)?[ \t]*\n(?P<patch>.*?)\n?```", re.IGNORECASE | re.DOTALL)


def _patch_text(rest: str) -> str:
    """The unified diff the lines after a patch request carry: one fenced block, or the lines themselves.
    ``""`` when they carry no complete one (a `---` line, a `+++` line and a hunk)."""
    fenced = list(_FENCED_PATCH_RE.finditer(rest))
    if fenced:
        if len(fenced) != 1 or rest[:fenced[0].start()].strip() or rest[fenced[0].end():].strip():
            return ""  # a request is its one diff: prose or a second block around it is not read
        patch = fenced[0].group("patch")
    else:
        patch = rest.strip("\n")
    lines = patch.splitlines()
    if not lines or not lines[0].startswith(("--- ", "diff ")):
        return ""
    if not any(line.startswith("+++ ") for line in lines) or not any(line.startswith("@@ ") for line in lines):
        return ""
    return patch if patch.endswith("\n") else patch + "\n"


def _patch(body: str, source_context: dict[str, Any] | None) -> MarkedFileAsk | None:
    """A unified diff the owner asks to apply, in the chat's own folder or in the folder the request names
    first ("in <folder>, apply this patch:"), when that folder is inside a marked folder: one
    `workspace.apply_unified_diff` placed there. Claims sweep (Windows): the pasted patch went to the model,
    which said "patched" and changed nothing. The diff's own paths are read by the patch tool, relative to
    that folder, and every gate (approval, the write gate, the patch tool's own path checks) decides them."""
    first, _newline, rest = body.partition("\n")
    lead = _LEAD_RE.match(first)
    pos = lead.end() if lead else 0
    named = ""
    target_file = ""
    missing = "There is no folder at `{path}`. "
    if _PATCH_VERB_RE.match(first, pos):
        # "apply this patch to C:\...\proj\notes.txt" (Steve R18): the full path the request names is
        # where the patch goes, a folder or a file in it, not the chat's own folder (an unbound chat's
        # Desktop, which then refused the patch for the wrong folder).
        found = next(((start, path) for start, path in _candidates(first) if start >= pos), None)
        if found is not None:
            start, named = found
            if not _local(first, start, named):
                return MarkedFileAsk("patch", refusal=_NOT_LOCAL.format(path=named) + _NOTHING_CHANGED)
            missing = "There is no file or folder at `{path}`. "
            try:
                if os.path.isfile(named) and not _is_link(named):
                    target_file, named = named, os.path.dirname(named)
            except (OSError, ValueError):
                pass
    else:
        folder_first = _FOLDER_FIRST_RE.match(first, pos)
        if folder_first is None:
            return None
        found = next(((start, path) for start, path in _candidates(first) if start == folder_first.end()), None)
        if found is None:
            return None
        start, named = found
        after = re.compile(r"[\"'`]?\s*,?\s*").match(first, start + len(named))
        if after is None or not _PATCH_VERB_RE.match(first, after.end()):
            return None
        if not _local(first, start, named):
            return MarkedFileAsk("patch", refusal=_NOT_LOCAL.format(path=named) + _NOTHING_CHANGED)
    patch = _patch_text(rest)
    if not patch:
        return None
    if named:
        try:
            is_dir = os.path.isdir(named) and not _is_link(named)
        except (OSError, ValueError):
            is_dir = False
        if not is_dir:
            return MarkedFileAsk("patch", refusal=missing.format(path=named) + _NOTHING_CHANGED)
        if target_file and not windows_write_gate.inside_flagged(_real_folder(target_file)):
            return MarkedFileAsk("patch", refusal=_NOT_MARKED.format(path=target_file) + _NOTHING_CHANGED)
        folder = _real_folder(named)
    else:
        context = source_context or {}
        own = str(context.get("workspace") or context.get("workspace_root") or "").strip()
        if not own:
            return None  # no folder of its own: the chat's workspace lanes read it, as before
        named, folder = own, _real_folder(own)
    if not folder or not windows_write_gate.inside_flagged(folder):
        return MarkedFileAsk("patch", refusal=_NOT_MARKED.format(path=named) + _NOTHING_CHANGED)
    shared = _shared_patch_target(patch)
    if shared:
        return MarkedFileAsk("patch", refusal=shared)
    return MarkedFileAsk("patch", intent="workspace.apply_unified_diff", workspace=folder, arguments={"patch": patch})


def _shared_patch_target(patch: str) -> str:
    """The refusal for a patch that names a share or device path as a file it changes (Steve R21: a
    `+++ \\\\localhost\\c$\\x.txt` got an approval card), in the words every other lane uses; "" otherwise.
    Read from the diff's names alone (the patch tool's own list), with nothing looked up: looking up
    `\\\\localhost\\c$` already opens the network connection. The patch tool's own refusal stays the backstop.
    Windows only (`windows_path_policy.active()`)."""
    from core import windows_path_policy

    if not windows_path_policy.active():
        return ""
    try:
        from core.execution.workspace_tools import patch_review_paths

        names = patch_review_paths(patch)
    except Exception:
        return ""
    for name in names:
        kind = windows_path_policy.refused_form(name, network_only=True) or windows_path_policy.refused_doubled_drive(name)
        if kind:
            return windows_path_policy.refusal_text(name, kind, nothing="written")
    return ""


def _first_unlookable(body: str, candidates: list[tuple[int, str]]) -> str:
    for start, path in candidates:
        if not _local(body, start, path):
            return _NOT_LOCAL.format(path=path)
    return ""


def _move(text: str) -> MarkedFileAsk | None:
    candidates = _candidates(text)
    if not candidates or not _MOVE_VERB_RE.match(text[:candidates[0][0]].rstrip("\"'`")):
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
    from core.execution.artifacts import latest_marked_undo_record_for_session

    none_left = (
        "There is no change VOOL made in a marked folder in this chat that is left to undo. "
        + _NOTHING_CHANGED
    )
    if not session_id:
        return MarkedFileAsk("undo", refusal=none_left)
    try:
        record = latest_marked_undo_record_for_session(session_id)
    except Exception:  # an unreadable ledger restores nothing on a guess
        return MarkedFileAsk("undo", refusal="I could not read this chat's change record. " + _NOTHING_CHANGED)
    if record is None:
        return MarkedFileAsk("undo", refusal=none_left)
    if isinstance(record.get("move"), dict):
        return _undo_move(record)
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


def _undo_move(record: dict[str, Any]) -> MarkedFileAsk:
    """The move back of a file VOOL moved between two marked folders: `machine.move_path` from where it
    went to where it was, judged as a move the owner asked for (both places still marked, links
    followed, the old place free), and through the same approval."""
    move = dict(record.get("move") or {})
    moved, original = str(move.get("destination") or ""), str(move.get("source") or "")
    refused = "Nothing was moved back. " + _NOTHING_CHANGED
    if not (os.path.isabs(moved) and os.path.isabs(original)):
        return MarkedFileAsk("undo", refusal="I could not read this chat's change record. " + _NOTHING_CHANGED)
    folder, name, refusal = _marked_file(moved)
    if refusal:
        return MarkedFileAsk("undo", refusal=f"The last change VOOL made in this chat moved a file to `{moved}`. "
                             + refusal + refused)
    parent = _real_folder(os.path.dirname(original))
    final = os.path.join(parent, os.path.basename(original)) if parent else ""
    if not parent or not os.path.isdir(parent) or not windows_write_gate.inside_flagged(parent) \
            or not windows_write_gate.inside_flagged(final):
        return MarkedFileAsk(
            "undo",
            refusal=(f"The last change VOOL made in this chat moved `{original}` to `{moved}`, and `{original}` is "
                     "not in a folder you have marked now, so I won't move it back there. " + refused),
        )
    if os.path.lexists(final):
        return MarkedFileAsk("undo", refusal=f"`{final}` already exists, so I won't move `{moved}` back over it. "
                             + refused)
    return MarkedFileAsk(
        "undo", intent="machine.move_path", workspace=folder,
        arguments={"source": os.path.join(folder, name), "destination": final},
        undoes=str(record.get("mutation_id") or ""),
    )


def moved_back(ask: MarkedFileAsk, execution: Any, *, session_id: str) -> None:
    """After an undo's move back ran: the move it undid, and the move back, are no longer left to undo."""
    if not ask.undoes or not bool(getattr(execution, "ok", False)):
        return
    if str(getattr(execution, "status", "") or "") != "moved":
        return
    from core.execution.artifacts import mark_marked_move_undone

    details = dict(getattr(execution, "details", {}) or {})
    mark_marked_move_undone(
        session_id=session_id, mutation_id=ask.undoes,
        source=str(details.get("dest") or ask.arguments.get("destination") or ""),
        destination=str(details.get("source") or ask.arguments.get("source") or ""),
    )


def marked_file_ask(text: str, *, source_context: dict[str, Any] | None, session_id: str = "") -> MarkedFileAsk | None:
    """What this turn asks of a file in a marked folder, or None for a turn this lane leaves alone:
    off Windows, a turn that is not the owner's own local one, and any other ask."""
    if not windows_write_gate.active() or not _owner_local(source_context):
        return None
    body = str(text or "")
    if "\n" in body.strip():
        # The one ask that carries several lines: a patch, with its request on the first line.
        return _patch(body.strip().replace("\r\n", "\n"), source_context)
    if not body.strip() or len(body.strip().splitlines()) > 1:
        # A request is one line; a paste of several is never read as one. `splitlines` also breaks
        # at \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and U+2029, not only at CR and LF.
        return None
    ledger_session = str((source_context or {}).get("session_id") or "").strip() or str(session_id or "").strip()
    for reader in (lambda: _undo(body, ledger_session), lambda: _move(body), lambda: _edit(body), lambda: _write(body)):
        ask = reader()
        if ask is not None:
            return ask
    return None


def carries_a_patch_request(text: str) -> bool:
    """On Windows, whether a message is a patch request (`_patch`): a request to apply a patch on its first
    line, then one complete unified diff. Read from the words alone, with nothing looked up. Its lines are
    one diff, so the turn is not split into a request per line. Off Windows, always False."""
    if not windows_write_gate.active():
        return False
    body = str(text or "").strip().replace("\r\n", "\n")
    first, newline, rest = body.partition("\n")
    if not newline:
        return False
    lead = _LEAD_RE.match(first)
    pos = lead.end() if lead else 0
    folder_first = _FOLDER_FIRST_RE.match(first, pos)
    verb = _PATCH_VERB_RE.match(first, pos) or (
        folder_first is not None and re.search(r"[\s,](?:" + _PATCH_VERB_RE.pattern + ")", first[folder_first.end():], re.IGNORECASE)
    )
    return bool(verb) and bool(_patch_text(rest))


def names_a_file_path(text: str) -> bool:
    """On Windows, whether a message names a full path to a file or folder: such a message is about that
    path, so the memory command does not read it as a correction of a remembered value (hardware captest,
    0.7.1: "replace the contents of C:\\Users\\Satoshi\\VOOL HW Test\\m3b.txt with the text v2" was stored as
    "Current corrected value: VOOL HW", and the file was left as it was). Off Windows, always False."""
    if not windows_write_gate.active():
        return False
    from core.agent_runtime import write_root_honesty as paths

    try:
        # Any candidate at all answers the question, so nothing is looked up on disk to read where a
        # path through folders with spaces ends (a share or a mapped drive is never touched here).
        return any(True for _ in paths._destination_candidates(str(text or ""), typed_shares=True, look_up=False))
    except Exception:  # a message this reader cannot read is left to the memory command, as before
        return False


def placed_context(ask: MarkedFileAsk, source_context: dict[str, Any] | None) -> dict[str, Any]:
    """The source context for the ask's one call: the chat's own, with its workspace set to the folder
    the call acts in."""
    context = dict(source_context or {})
    context["workspace"] = ask.workspace
    context["workspace_root"] = ask.workspace
    return context


__all__ = ["MarkedFileAsk", "carries_a_patch_request", "marked_file_ask", "moved_back", "names_a_file_path", "placed_context"]
