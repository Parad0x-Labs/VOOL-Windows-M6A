"""Windows: a Python or pytest ask that names a marked folder works in that folder.

Captest C1-C3 (installed 0.7.1-beta, 2026-11-09). An unbound chat works in the Desktop. Asked to
"run calc.py in C:\\Users\\VOOLT\\VOOL Test\\Allowed Ä folder" (a folder the owner marked in Settings
→ Folders VOOL may change), every tool the model chose was refused as "outside the active workspace
(Desktop)", and the reply told the owner to open a chat bound to the other folder. Marked folders
are already the only places VOOL may change on Windows, and the sandbox runs only Python and pytest,
confined to the chat's folder. So an unbound chat's code ask that names a marked folder, or a file
in one, works in that folder for the turn: the tools, the sandbox job's working directory and the
code task are all placed there, and every gate still decides each call (approval, the write gate,
the Python/pytest-only launcher).

Only a folder inside a marked folder is ever chosen. An unmarked folder, a share or device path, a
path that does not exist, or two different folders named in one turn choose nothing, and the chat
keeps its own folder, as before. A turn that names no folder at all keeps the folder the chat's
latest message naming one chose, so "now run the tests" stays where "run calc.py in <folder>" ran.
Off Windows nothing changes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from core import windows_write_gate

#: The words of a Python or pytest ask: a `.py` file, or tests, pytest, Python or a script.
_CODE_ASK_RE = re.compile(
    r"\.py\b|(?<![\w.-])(?:pytest|python3?|tests?|test\s+suite|unit\s+tests?|scripts?)(?![\w-])",
    re.IGNORECASE,
)

#: The workspace binding the chat request records for a turn placed this way.
BINDING = "marked_folder"


def is_code_ask(text: str) -> bool:
    """Whether ``text`` asks about Python or tests, read without the paths it names: the captest's
    own folder is called "VOOL Test"."""
    from core.agent_runtime import write_root_honesty as paths

    body = str(text or "")
    for start, raw in reversed(paths._rooted_candidates(body)):
        if paths._clean_path(raw).lower().endswith(".py"):
            return True  # a Python file named by its full path
        body = body[:start] + " " + body[start + len(raw):]
    return bool(_CODE_ASK_RE.search(body))


def _named_folder(text: str) -> str | None:
    """The one marked folder ``text`` names (a folder, or the folder of a file it names).

    None when ``text`` names no full path at all; ``""`` when it names one but no single marked
    folder follows from it (unmarked, missing, a share or device path, or two different folders)."""
    from core.agent_runtime import write_root_honesty as paths

    body = str(text or "")
    candidates = list(paths._destination_candidates(body))
    if not candidates:
        return None
    chosen: list[str] = []
    for start, candidate in candidates:
        if paths.typed_path_kind(body, start, candidate):
            return ""  # a share, device or incomplete path is never looked up
        try:
            target = Path(candidate).expanduser()
            if not target.is_absolute():
                return ""
            folder = target if target.is_dir() else (target.parent if target.is_file() else None)
            if folder is None:
                return ""
            real = os.path.realpath(str(folder))
        except (OSError, ValueError):
            return ""
        # Where the folder really is, links and junctions followed: a link inside a marked folder
        # that leads out of it is not inside it.
        if not windows_write_gate.inside_flagged(real):
            return ""
        if all(os.path.normcase(real) != os.path.normcase(seen) for seen in chosen):
            chosen.append(real)
    return chosen[0] if len(chosen) == 1 else ""


def turn_folder(text: str, history: list[dict[str, Any]] | None = None) -> str:
    """The marked folder this turn's code ask works in, or ``""`` to keep the chat's own folder.

    ``history`` is the chat's messages, this turn's own last; only the user's messages count."""
    if not windows_write_gate.active() or not is_code_ask(text):
        return ""
    named = _named_folder(text)
    if named is not None:
        return named
    earlier = [str(item.get("content") or "") for item in list(history or [])
               if isinstance(item, dict) and str(item.get("role") or "") == "user"]
    if earlier and earlier[-1].strip() == str(text or "").strip():
        earlier = earlier[:-1]
    for message in reversed(earlier):
        named = _named_folder(message)
        if named is not None:
            return named
    return ""
