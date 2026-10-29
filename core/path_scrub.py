"""One shared recogniser for Windows-style local paths, including their escaped spellings.

A path that went through a Python ``repr`` or a JSON encoder carries doubled backslashes
(``C:\\\\Users\\\\alice\\\\f.py`` on the wire is ``C:\\Users\\alice\\f.py`` on disk). The redactors'
own patterns know a single backslash or a POSIX slash only, so the escaped spelling slipped past
all of them and kept the account name and folder layout. This module is the extra pass each
redactor runs IN ADDITION to its existing patterns -- it never replaces them.

Recognised: drive-absolute (``C:\\``, ``C:/``, ``C:\\\\``), UNC (``\\\\server\\share`` and the doubled
``\\\\\\\\server\\\\share``), long/device forms (``\\\\?\\C:\\...``, ``\\\\?\\UNC\\...``, ``\\\\.\\...``) and
a rooted ``\\Users\\...``. Every separator may be one backslash, two backslashes or ``/``.

Deliberately narrow so prose survives: a drive needs a single letter that is not the tail of a
longer word, a UNC server needs two or more host characters, and a bare relative or slashed
model id (``openrouter/anthropic/x``) is never a match. Every quantified run is over a class
that excludes the separators, so matching is linear (no nested quantifiers over the same
characters).
"""
from __future__ import annotations

import re
from collections.abc import Callable

#: One separator: two backslashes (escaped), one backslash, or a forward slash.
SEP = r"(?:\\\\|\\|/)"
_BS = r"(?:\\\\|\\)"
#: A path segment: anything that is not a separator, whitespace, a quote or a Windows-reserved char.
_SEG = r"[^\\/\s\"'<>|*?:]+"
_TAIL = _SEG + r"(?:" + SEP + _SEG + r")*" + SEP + r"?"

# The lookbehinds keep a match from starting in the middle of a longer path, or right after a
# redaction marker's closing "]" (``C:/Users/[redacted-user]\\AppData`` is already handled).
_DEVICE = (
    r"(?<![\w\\:\]])" + _BS + _BS + r"[?.]" + _BS
    + r"(?:[A-Za-z]:" + SEP + _TAIL + r"|" + _SEG + SEP + _TAIL + r")"
)
_DRIVE = r"(?<![A-Za-z0-9\\])[A-Za-z]:" + SEP + _TAIL
_UNC = r"(?<![\w\\:/.\]])(?:\\\\){1,2}[A-Za-z0-9._-]{2,}" + SEP + _TAIL
_ROOTED_USERS = r"(?<![\w\\:/.\]])" + _BS + r"(?i:users)" + SEP + _TAIL

#: The `\\?\...`, `\\.\...` and `\\server\share` forms: network and device paths.
NETWORK_PATH_RE = re.compile(r"(?:" + _DEVICE + r")|(?:" + _UNC + r")")
#: The device forms alone (distinctive enough to flag in a JSON-encoded payload).
DEVICE_PATH_RE = re.compile(_DEVICE)
#: Every form above, as one pattern (no capture groups: ``findall`` counts matches).
WINDOWS_PATH_RE = re.compile(
    r"(?:" + _DEVICE + r")|(?:" + _DRIVE + r")|(?:" + _UNC + r")|(?:" + _ROOTED_USERS + r")"
)

_SPLIT_RE = re.compile(r"[\\/]+")
_HOME_DIRS = frozenset({"users", "home"})


def path_parts(path: str) -> list[str]:
    """The non-empty segments of ``path`` split on any run of backslashes or slashes."""
    return [part for part in _SPLIT_RE.split(path) if part]


def path_basename(match: re.Match[str]) -> str:
    """The file name a matched path ends in, or ``<path>`` when nothing safe is left.

    A path that ends AT a home directory's account folder (``...\\Users\\alice``) would name the
    account as its "basename", so that case yields the placeholder instead.
    """
    parts = path_parts(match.group(0))
    if not parts:
        return "<path>"
    if len(parts) >= 2 and parts[-2].lower() in _HOME_DIRS:
        return "<path>"
    return parts[-1]


def scrub_windows_paths(text: str, replacement: str | Callable[[re.Match[str]], str]) -> str:
    """Replace every recognised Windows path in ``text``.

    ``replacement`` is a literal string (never interpreted for backslash escapes) or a callable
    taking the match, so each caller keeps its own placeholder.
    """
    if callable(replacement):
        return WINDOWS_PATH_RE.sub(replacement, text)
    return WINDOWS_PATH_RE.sub(lambda _match: replacement, text)


__all__ = [
    "DEVICE_PATH_RE",
    "NETWORK_PATH_RE",
    "SEP",
    "WINDOWS_PATH_RE",
    "path_basename",
    "path_parts",
    "scrub_windows_paths",
]
