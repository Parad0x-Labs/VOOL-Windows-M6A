"""Windows: a read the user types against a share or a device path is refused in plain words.

"read \\\\localhost\\c$\\Windows\\win.ini", "show me \\\\.\\PhysicalDrive0" and "open \\\\?\\C:\\" asked for a
file's contents, and no message lane claimed them: the read lane (`machine_file_read_path`) never
looks a share or a device up, by design, so the turn went to the model. The tool door then refused
the call if the model made one, and the model paraphrased that refusal or never made the call, so
the user got a vague answer. Measured on Windows 11, 11 Nov 2026 (claims sweep, 170f6572). Nothing was
read either way.

`non_local_read_target` reads the message as typed with the read lane's own cue (or a listing verb:
list, ls, dir, browse), and names the first
path in it that is a share or a device. The front door answers that path with the same refusal
sentence the tool door gives. It is lexical: nothing is looked up, resolved or opened. Off Windows it
returns ("", "") at its first statement.

Only shares and devices are refused here. A root-relative or drive-relative spelling is not a full
local path, but it is also how a POSIX path or a sentence fragment reads, and the read and list
tools' own door already refuses it in the same words.

`climbing_relative_read` is the read side of the write lane's `..` rule (owner decision D4, 11 Nov
2026): a read whose relative path climbs with `..` out of the chat's own folder is refused in plain
words, naming where it leads, placed by text from the chat's folder. A climb that stays inside it is
left alone.
"""
from __future__ import annotations

import re

from core import windows_path_policy

_TRAILING = ".,;:!?)"
# A listing is a read of a folder: "list \\?\C:\" reached the model (Steve round 18, 12 Nov 2026).
_LISTING_CUE_RE = re.compile(r"\b(?:list|lists|listing|ls|dir|browse)\b", re.IGNORECASE)


def non_local_read_target(text: str) -> tuple[str, str]:
    """On Windows, the first path a read in this message names that is a share or a device, with its
    kind; ("", "") otherwise, and always off Windows. Nothing is looked up."""
    if not windows_path_policy.active():
        return "", ""
    from core.agent_runtime.write_root_honesty import _TYPED_WORD_RE, typed_path_kind

    body = str(text or "")
    if not (_asks_to_read(body) or _asks_to_list(body)):
        return "", ""
    for match in _TYPED_WORD_RE.finditer(body):
        group = "quoted" if match.group("quoted") is not None else "word"
        candidate = str(match.group(group) or "").strip().rstrip(_TRAILING)
        if not candidate:
            continue
        kind = typed_path_kind(body, match.start(group), candidate)
        if kind in windows_path_policy.NETWORK_KINDS:
            return candidate, kind
    return "", ""


def _asks_to_read(body: str) -> bool:
    from core.execution.constants import _TYPED_FILE_CONTENT_CUE_RE, _TYPED_FILE_OTHER_INTENT_RE

    return bool(_TYPED_FILE_CONTENT_CUE_RE.search(body)) and not _TYPED_FILE_OTHER_INTENT_RE.search(body)


def _asks_to_list(body: str) -> bool:
    from core.execution.constants import _TYPED_FILE_OTHER_INTENT_RE

    return bool(_LISTING_CUE_RE.search(body)) and not _TYPED_FILE_OTHER_INTENT_RE.search(body)


def climbing_relative_read(text: str, *, source_context: dict[str, object] | None) -> tuple[str, str]:
    """On Windows, ``(path, leads_to)`` for the first relative path with a `..` name that a read in this
    message names and that, placed from the chat's folder, leads outside it; ("", "") otherwise, and
    always off Windows. Nothing is looked up."""
    if not windows_path_policy.active():
        return "", ""
    import os

    from core.agent_runtime.write_root_honesty import _chat_folder, _placed_from, _relative_climbs

    body = str(text or "")
    if not body.strip() or not _asks_to_read(body):
        return "", ""
    climbs = _relative_climbs(body)
    if not climbs:
        return "", ""
    folder = _chat_folder(source_context)
    if not folder or windows_path_policy.refused_form(folder, network_only=True):
        return "", ""
    base = os.path.normcase(os.path.normpath(folder))
    for _start, path in climbs:
        leads_to = _placed_from(folder, path)
        placed = os.path.normcase(leads_to)
        if placed == base or placed.startswith(base.rstrip("\\/") + os.sep):
            continue  # the climb stays inside the chat's folder
        return path, leads_to
    return "", ""


def climbing_relative_read_response(path: str, leads_to: str) -> str:
    """The plain refusal for `climbing_relative_read`."""
    return (
        f"I can't open `{path}`: its `..` climbs out of this chat's folder, so it leads to `{leads_to}`. "
        "On Windows, give the full path instead (like C:\\Users\\you\\file.txt). Nothing was opened."
    )


__all__ = ["climbing_relative_read", "climbing_relative_read_response", "non_local_read_target"]
