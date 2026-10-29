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
#: The ACCOUNT segment of a home directory (``Users\John Smith``, ``Users\al-ice.o'neil``): the one
#: place a space or an apostrophe is part of the name. It is at most three words joined by single
#: spaces (no leading, trailing or doubled space), at most 64 characters, and it must END right at
#: a separator, at a line end, before a ``,`` ``;`` or a closing quote or bracket, before a full stop
#: that ends a sentence, or at the end of the text, so prose after a plain name
#: (``Users\bob failed to start``, ``Users\bob then run x/y``) is never swallowed.
#: Every class excludes the separators and whitespace, so matching is linear (no nested unbounded
#: quantifiers); anything else falls back to the strict ``_SEG``.
#: One word of the account. An apostrophe or a dot counts only between two name characters
#: (``al-ice.o'neil``), and a closing bracket never does, so the quote, bracket or full stop that
#: closes a value or a sentence is never read as part of the name.
_ACCT_CHAR = r"[^\\/\s\"'.<>|*?:,;)\]}]"
_ACCT_WORD = _ACCT_CHAR + r"+(?:['.]" + _ACCT_CHAR + r"+)*"
#: Where an account may end without a separator: a line end, ``,`` ``;``, a closing quote or bracket
#: (a JSON ``"cwd": "C:\\Users\\Bob Smith"`` ends at its ``"``), a full stop that ends a sentence,
#: or the end of the text.
_CLOSERS = "\"')\\]}>"
_ACCT_STOP = r"(?=[,;\r\n" + _CLOSERS + r"]|\.(?:\s|$)|$)"
_ACCT = (
    r"(?=[^\\/\r\n,;" + _CLOSERS + r"]{1,64}(?:[\\/,;\r\n" + _CLOSERS + r"]|\.(?:\s|$)|$))"
    # lazy: the first word count that reaches a stop wins, so ". Retry" after a name stays prose
    + _ACCT_WORD + r"(?: " + _ACCT_WORD + r"){0,2}?"
    + r"(?:(?=[\\/])|" + _ACCT_STOP + r")"
)
#: The same segment, but only when the name actually holds a space or an apostrophe (what the
#: strict ``_SEG`` cannot take), so names without either keep today's handling.
_ACCT_SPACED = r"(?=[^\\/\r\n]{0,64}[ '])" + _ACCT
#: What follows ``Users`` + separator: the account up to the next separator (then the rest of the
#: path), the account ending at a line end, ``,`` ``;`` or the end of the text, or -- when neither
#: holds -- the strict tail.
_USERS_REST = (
    r"(?i:users)" + SEP
    + r"(?:" + _ACCT + r"(?:" + SEP + r"(?:" + _TAIL + r")?|" + _ACCT_STOP + r")|" + _TAIL + r")"
)
#: What may follow a prefix (drive, UNC server/share, device): a home directory, or any path.
_AFTER_PREFIX = r"(?:" + _USERS_REST + r"|" + _TAIL + r")"

# The lookbehinds keep a match from starting in the middle of a longer path, or right after a
# redaction marker's closing "]" (``C:/Users/[redacted-user]\\AppData`` is already handled).
_DEVICE = (
    r"(?<![\w\\:\]])" + _BS + _BS + r"[?.]" + _BS
    + r"(?:[A-Za-z]:" + SEP + _AFTER_PREFIX + r"|" + _SEG + SEP + _AFTER_PREFIX + r")"
)
_DRIVE = r"(?<![A-Za-z0-9\\])[A-Za-z]:" + SEP + _AFTER_PREFIX
_UNC = r"(?<![\w\\:/.\]])(?:\\\\){1,2}[A-Za-z0-9._-]{2,}" + SEP + _AFTER_PREFIX
_ROOTED_USERS = r"(?<![\w\\:/.\]])" + _BS + _USERS_REST
#: POSIX ``/home/<name>`` and ``/Users/<name>`` whose name holds a space or an apostrophe.
_POSIX_SPACED_HOME = (
    r"(?<![\w/\\.:\]])/(?i:home|users)/" + _ACCT_SPACED
    + r"(?:/(?:" + _SEG + r"(?:/" + _SEG + r")*/?)?|" + _ACCT_STOP + r")"
)

#: The `\\?\...`, `\\.\...` and `\\server\share` forms: network and device paths.
NETWORK_PATH_RE = re.compile(r"(?:" + _DEVICE + r")|(?:" + _UNC + r")")
#: The device forms alone (distinctive enough to flag in a JSON-encoded payload).
DEVICE_PATH_RE = re.compile(_DEVICE)
#: Every form above, as one pattern (no capture groups: ``findall`` counts matches).
WINDOWS_PATH_RE = re.compile(
    r"(?:" + _DEVICE + r")|(?:" + _DRIVE + r")|(?:" + _UNC + r")|(?:" + _ROOTED_USERS + r")|(?:"
    + _POSIX_SPACED_HOME + r")"
)
#: ``[drive:]<sep>users|home<sep><account>`` (groups: drive, users|home) for an account name that
#: holds a space or an apostrophe, up to its closing separator; for redactors that mask just the
#: home prefix. Runs BEFORE their strict-account patterns, which would stop inside the name.
SPACED_HOME_RE = re.compile(
    r"(?:(?<![A-Za-z0-9\\])([A-Za-z]:))?" + SEP + r"(users|home)" + SEP + _ACCT_SPACED,
    re.IGNORECASE,
)

_SPLIT_RE = re.compile(r"[\\/]+")
#: Folders whose children are accounts: local homes, and the profile shares a server publishes
#: (``\\\\fs01\\users$``, ``\\\\fs01\\profiles``), with or without the hidden-share ``$``.
_HOME_DIRS = frozenset({"users", "home", "homes", "profiles"})


def path_parts(path: str) -> list[str]:
    """The non-empty segments of ``path`` split on any run of backslashes or slashes."""
    return [part for part in _SPLIT_RE.split(path) if part]


def ends_at_home_account(parts: list[str]) -> bool:
    """Whether a path split into ``parts`` ends AT a home directory's account folder
    (``.../Users/alice``, ``/home/alice/``), where its last segment is the account name itself."""
    return len(parts) >= 2 and parts[-2].lower().rstrip("$") in _HOME_DIRS


def path_basename(match: re.Match[str]) -> str:
    """The file name a matched path ends in, or ``<path>`` when nothing safe is left.

    A path that ends AT a home directory's account folder (``...\\Users\\alice``) would name the
    account as its "basename", so that case yields the placeholder instead.
    """
    text = match.group(0)
    parts = path_parts(text)
    if not parts:
        return "<path>"
    if ends_at_home_account(parts):
        return "<path>"
    if text[:1] == "\\" and text[1:2] == "\\":
        # A share path: its first segments are the server, the share and the folder in it, which
        # name a person as often as a file does (``\\\\fs01\\jsmith$``, ``\\\\fs01\\share\\jsmith``).
        named = parts[2:] if parts[0] in ("?", ".") else parts
        if named and named[0].lower() == "unc":
            named = named[1:]
        if len(named) <= 3:
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
    "SPACED_HOME_RE",
    "WINDOWS_PATH_RE",
    "ends_at_home_account",
    "path_basename",
    "path_parts",
    "scrub_windows_paths",
]
