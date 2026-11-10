"""Refuse a write this build cannot reach, instead of quietly performing it somewhere else.

Found by a blind QA drive on 2026-07-29. Asked to ``create /tmp/vool_qa_build6/notes.md with a
bulleted summary ...``, the runtime wrote ``~/Documents/notes.md`` and answered "Created file
``~/Documents/notes.md``." The write really happened, a real tool receipt backed it, and every
honesty guard passed: the reply was true about what it did and silent about that not being what was
asked. Silent relocation is worse than refusal -- the user is told a file exists, it does exist, and
it is nowhere they will look for it.

So the rule here is narrow and absolute: when the request roots its own path -- absolute POSIX,
``~``-relative, or a Windows drive path -- the answer is either that path or an explanation naming
the roots we can reach. Never a different path presented as though it were the one asked for.

The gate is deliberately conservative about what counts as a write. A rooted path alone is not
enough (``take a look inside /Users/me/Desktop/ledger-demo`` is a read), and a write verb anywhere
in the sentence is not enough either (``read /etc/hosts and make a summary`` writes nothing). The
verb must come BEFORE the path and close to it, which is how a person names a destination.
"""
from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

from core import windows_known_folders, windows_path_policy
from core.execution.constants import drive_left_behind

# A path the USER rooted. The lookbehind is what keeps prose out: the slash in "and/or", "24/7",
# "TCP/IP" and "he/she" is preceded by a word character and never starts a path. It also has to keep
# out a slash that is only ROOTED-LOOKING because something else sits right before it: "." is the
# real case found on 2026-08-07 -- "./foo.txt" and "../sibling.txt" both have their "/" preceded by
# a bare period, which the old class didn't exclude, so the match started AT that slash and reported
# the relative path as if it were the absolute path "/foo.txt" or "/sibling.txt". A workspace-
# relative path is never what this gate is for; excluding "." keeps it out the same way "-" already
# keeps "well-/badly-formed" out.
_ROOTED_PATH_RE = re.compile(
    r"(?<![\w`'\"/~\\.-])(?P<path>(?:~(?=[/\\])|[A-Za-z]:[\\/]|/)[^\s`'\"<>|]*)"
)
# A share, device or NT path as a Windows user types it, with backslashes, which `_ROOTED_PATH_RE`
# never starts at: its alternatives start at `~`, a drive or `/`, so `\\host\share\x`, `\\?\C:\x`,
# `\\.\pipe\x` and `\??\UNC\host` were no candidate at all, and a write to one fell through to the
# builder or the model (clean-VM capability test B10, 0.7.1-beta). A share needs a host and a separator
# after it, so an escape such as `\\d+` or `\\n` is no share. A quote or backtick may sit right before it
# (`"\\host\s\x"`). Only the write finder behind the Windows refusal reads these
# (`non_local_write_target`); what the reachability check reads is unchanged.
_TYPED_SHARE_RE = re.compile(
    r"(?<![\w/~\\.-])(?P<path>(?:\\\\(?:[?.][\\/]|[^\\/\s`'\"<>|]+[\\/])|\\\?\?\\)[^\s`'\"<>|]*)"
)
# ANVIL round 2, 2026-08-07: the first D16 fix truncated the scan at the first guessed content
# marker. That is directional and single-shot -- it missed every shebang that had no marker word
# in front of it ("create ./b.sh\n#!/bin/sh", "make ./a.sh executable, content #!/usr/bin/env
# python3", "create ./d.sh - #!/bin/zsh"), and it discarded any REAL destination that happened to
# be named AFTER the first marker found anywhere in the turn ("I have a folder containing: old
# logs. Now create /tmp/x" truncated before "/tmp/x" ever ran). Replaced below with per-candidate
# classification: each rooted match is judged by its OWN local context, not by where the first
# marker in the whole turn happened to sit.
#
# A rooted-looking string on a code line, in a code fence, or inside a marker's content SPAN is
# PAYLOAD, not a destination -- regardless of where else in the turn a genuine destination is
# named. See `_is_payload_span`.
_CODE_FENCE_RE = re.compile("```")
# ANVIL round 3, 2026-08-07 (R1): a clause boundary (below, now removed) ended a marker's reach at
# the next newline or sentence -- correct for "containing: see /etc/motd for the banner text" on
# one line, wrong for dictated MULTI-LINE content ("...whose contents contains:\n\nprefix=/opt/app
# \nPREFIX=/usr/local\n..."), where line 2 onward is not shebang-adjacent and was mined as a
# destination the moment it crossed a newline. A content marker's span now runs from the marker to
# the first DESTINATION-PREPOSITION-governed candidate after it (see `_DESTINATION_PREPOSITION_RE`)
# -- that is what a real second destination looks like ("...contents: hello at /tmp/foo.txt",
# "...\n\nNow also save a backup at /tmp/backup.ini") -- or to the end of the text if no such
# candidate follows, which is what genuinely-multi-line dictated content needs. See
# `_content_marker_spans`. Vocabulary expanded (colon-optional "containing", "that mentions",
# "contents are", "add a line") for the natural unmarked forms ANVIL attacked directly, without
# growing into an open-ended command/verb/path blacklist -- these are still a small, closed set of
# content-introducing phrases, not a list of forbidden paths or programs.
_CONTENT_MARKER_RE = re.compile(
    r"\bwith\s+(?:the\s+|these\s+|this\s+|exact(?:ly)?\s+)?(?:contents?|text|code|lines?|body)\s*:"
    r"|\bcontaining\s*:?"
    r"|\bcontents?\s+(?:is|are|contains)\b"
    r"|\bthat\s+(?:says|reads|mentions)\s*:?"
    r"|\bsaying\s*:"
    r"|\bwrite\s+this\s*:"
    r"|\bcontent\s*:"
    r"|\badd(?:ing)?\s+a\s+lines?\b",
    re.IGNORECASE,
)
# The structural signal that a real destination follows dictated content, closing that content's
# span right before it: "at", "to", "into", "onto", "under", "inside (of)" immediately ahead of a
# candidate is how a person names a PLACE, not more content. A closed class of English function
# words, not a growing blacklist.
_DESTINATION_PREPOSITION_RE = re.compile(
    r"\b(?:at|to|into|onto|under|inside(?:\s+of)?)\s*\Z", re.IGNORECASE
)
# The other way a content span ends: a genuinely NEW sentence after the marker, governed by its
# OWN write-intent verb ("I have a folder containing: old logs. Now create /tmp/x" -- "Now
# create" is a fresh instruction, not more of the "old logs" content). Requires an actual sentence
# break in between, not just any later verb-shaped word -- a note whose dictated prose happens to
# contain "add" or "create" mid-sentence is not thereby a fresh instruction.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]\s")
# F7, ANVIL round 2: "create a config from https://example.com/x" mined "//example.com/x" as a
# rooted destination -- the colon before the double-slash wasn't excluded by the lookbehind above
# (excluding it there would also break the legitimate, if rare, "label:/path" phrasing that has no
# double slash). A URL authority section is recognized structurally instead: a candidate starting
# with "//" immediately after a URI scheme is a network resource, never a filesystem path.
_URL_SCHEME_BEFORE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\Z")
# A destination-naming verb. "need/want a file at ..." earns its place: it is how the QA drive
# phrased build 13 ("I need a file at /tmp/vool_qa_build13/config.yaml with three sample keys"),
# and with no verb in it that request was read as a REQUEST TO READ the file.
_WRITE_INTENT_RE = re.compile(
    r"\b(?:creat(?:e|ing)|mak(?:e|ing)|writ(?:e|ing)|sav(?:e|ing)|put(?:ting)?|plac(?:e|ing)|"
    r"add(?:ing)?|generat(?:e|ing)|scaffold(?:ing)?|bootstrap(?:ping)?|mkdir|touch|drop|append|"
    r"stor(?:e|ing)|set\s+up|setting\s+up|"
    r"need(?:ed)?\s+(?:a|an|the)\s+\w*\s*file|want(?:ed)?\s+(?:a|an|the)\s+\w*\s*file)\b",
    re.IGNORECASE,
)
# How far a destination verb may sit ahead of the path it governs. "put it at /tmp/x.sh" is 10
# characters; "...and then I finally decided to read /etc/hosts" keeps `make` from a later clause
# out of range.
_WRITE_INTENT_WINDOW = 60
# A question about what the USER should do, not an instruction to us: "should I create
# /etc/nginx/nginx.conf myself or use the package default?" First person only -- "can you go ahead
# and set up /tmp/..." and "Could you please create a file ... in /tmp/..." are both real requests
# and must still be answered about their own path.
_USER_OWN_ACTION_RE = re.compile(
    r"\b(?:should|shall|can|could|do|did|would|must|ought)\s+i\b"
    r"|\bi\s+(?:should|could|would|ought\s+to)\s+"
    r"(?:creat|writ|mak|add|put|sav|plac|generat|scaffold)",
    re.IGNORECASE,
)

_SAFE_MACHINE_ROOT_NAMES = ("Desktop", "Downloads", "Documents")


def _clean_path(candidate: str) -> str:
    """The path without the sentence punctuation after it. A trailing `..` is the parent folder, not
    punctuation: stripped, "create x.txt in <marked>\\.." was read as the marked folder itself and the
    file was written there instead of in the parent the user named (captest B6, 2026-11-09). `x\\...`
    is that climb and a full stop."""
    text = str(candidate or "").strip()
    cleaned = text.rstrip(".,;:!?)")
    if (not cleaned or cleaned.endswith(("/", "\\"))) and text[len(cleaned):].startswith(".."):
        return cleaned + ".."
    return cleaned.strip()


def _is_url_authority(text: str, start: int) -> bool:
    """Whether the match at `start` is a URL's `//host/...` authority section: a network
    resource, not a filesystem path, no matter what governs it."""
    return text[start : start + 2] == "//" and bool(_URL_SCHEME_BEFORE_RE.search(text[:start]))


def requested_rooted_paths(text: str) -> list[str]:
    """Every path in the text that the user rooted, in the order they appear. A URL's authority
    section (`https://host/...`) is never included -- it names a network resource, not a
    filesystem destination."""
    body = str(text or "")
    found: list[str] = []
    for match in _ROOTED_PATH_RE.finditer(body):
        if _is_url_authority(body, match.start()):
            continue
        candidate = _clean_path(match.group("path"))
        if len(candidate) > 1 and candidate not in found:
            found.append(candidate)
    return found


def allowed_write_roots(source_context: dict[str, object] | None) -> list[Path]:
    """Directories this build may write into: the active workspace and the safe machine roots."""
    roots: list[Path] = []
    workspace = str(
        (source_context or {}).get("workspace") or (source_context or {}).get("workspace_root") or ""
    ).strip()
    if not workspace:
        try:
            from core.runtime_paths import active_workspace_dir

            workspace = str(active_workspace_dir())
        except Exception:
            workspace = ""
    # On Windows a workspace on a share is no root this build writes to, and is not resolved to
    # find out: resolving `\\host\share` is already the SMB connection.
    if workspace and not windows_path_policy.refused_form(workspace, network_only=True):
        with contextlib.suppress(Exception):
            roots.append(Path(workspace).expanduser().resolve())
    home = Path.home()
    for name in _SAFE_MACHINE_ROOT_NAMES:
        try:
            roots.append(windows_known_folders.safe_folder(name, home).resolve())
        except Exception:
            continue
    return _windows_writable(roots)


def _windows_writable(roots: list[Path]) -> list[Path]:
    """On Windows only folders marked disposable can be written (core/windows_write_gate.py), so the
    writable roots are exactly the marked folders, wherever they are. Clean-VM job 52 was offered
    `~/Desktop` and `~/Downloads`, which the gate then refused because neither was marked. Captest B3
    (2026-11-08) was the other half: the roots used to be cut to where a marked folder overlapped
    the workspace or Desktop, Documents and Downloads, so a folder marked anywhere else was dropped
    and chat answered "no folder I can write to is marked yet" while Settings showed it marked.
    Marked folders inside one of ``roots`` are listed first. Off Windows the roots stay as they are."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return roots
    try:
        flagged = [Path(root) for root in windows_write_gate.flagged_roots()]
    except Exception:
        return []

    def _under(child: Path, parent: Path) -> bool:
        c, p = os.path.normcase(str(child)), os.path.normcase(str(parent)).rstrip("\\/")
        return c == p or c.startswith(p + "\\") or c.startswith(p + "/")

    inside_a_root = [marked for marked in flagged if any(_under(marked, root) for root in roots)]
    writable: list[Path] = []
    for marked in inside_a_root + flagged:
        if all(os.path.normcase(str(marked)) != os.path.normcase(str(w)) for w in writable):
            writable.append(marked)
    return writable


def typed_path_kind(text: str, start: int, candidate: str) -> str:
    """On Windows, which kind of non-local path ``candidate`` is, judged as the user typed it: ``""``
    for a full local path or a relative one. Always ``""`` off Windows.

    ``candidate`` starts at ``start`` in ``text``. A drive letter left just before it belongs to it
    (``C: /x`` is the ``C:/x`` the user typed), so a root-relative candidate there is that drive's
    full path. Nothing else rescues a candidate: a share is what a lookup of the candidate itself
    would reach, whatever was typed before it.
    """
    kind = windows_path_policy.refused_form(candidate) or windows_path_policy.refused_doubled_drive(candidate)
    if kind == "root_relative" and drive_left_behind(text, start, candidate):
        return ""
    return kind


def _is_reachable(candidate: str, roots: list[Path]) -> bool:
    try:
        resolved = Path(candidate).expanduser().resolve()
    except Exception:
        return True  # unparseable: not our call to make, let the normal lanes handle it
    return any(resolved == root or root in resolved.parents for root in roots)


def _write_intent_precedes(text: str, start: int) -> bool:
    """Whether a destination verb sits just before position `start`, rather than anywhere in the
    turn. Position-based (not a substring re-search) so a candidate that appears more than once,
    once as payload and once as a real destination, is judged on its OWN occurrence."""
    window = text[max(0, start - _WRITE_INTENT_WINDOW) : start]
    return bool(_WRITE_INTENT_RE.search(window) and not _USER_OWN_ACTION_RE.search(window))


def _write_intent_governs(text: str, start: int, passed_start: int) -> bool:
    """Whether a destination verb governs the candidate at ``start``: one just before it, or one just
    before the first candidate the check passed over earlier in the turn, at ``passed_start`` (-1 for
    none). Base answered the turn at that candidate, so a path the turn names after it, past the
    verb's reach (``write hello ... to /*x*/ //host/s``), is answered in its place."""
    if _write_intent_precedes(text, start):
        return True
    return passed_start >= 0 and _write_intent_precedes(text, passed_start)


# Where a share, a device or a URL can start inside one typed path: a doubled separator
# (`/*x*///host/s`, `/*x*/\\host\s`), a `:` (`/*x*/file://host/s`) or a `?` (`/*x*/??/UNC/host/s`).
_SHARE_CAN_START_INSIDE_RE = re.compile(r"[\\/]{2}|[:?]")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) character spans of every COMPLETE ``` ... ``` fence in `text`. An unpaired
    trailing ``` opens no span -- nothing after it is fenced by an opener that never closed."""
    marks = [match.start() for match in _CODE_FENCE_RE.finditer(text)]
    return [(marks[i], marks[i + 1]) for i in range(0, len(marks) - 1, 2)]


def _is_inside_fence(start: int, fence_spans: list[tuple[int, int]]) -> bool:
    return any(fence_start <= start <= fence_end for fence_start, fence_end in fence_spans)


_SHEBANG_ADJACENT_RE = re.compile(r"#!\s{0,4}\Z")


def _is_shebang_adjacent(text: str, start: int) -> bool:
    """Whether `#!` sits directly before this candidate, POSIX-legal optional whitespace and all.
    A shebang is strong payload evidence regardless of what punctuation or words surround it on
    the line -- "#!/bin/sh" on its own line, "#! /bin/sh" (the POSIX-legal space form), "content
    #!/usr/bin/env python3" inline, and "- #!/bin/zsh" after a dash all qualify, because what
    comes immediately before the match is what a shebang always is, not where it sits in the
    sentence."""
    return bool(_SHEBANG_ADJACENT_RE.search(text[max(0, start - 6) : start]))


def _is_destination_preposition_governed(text: str, start: int) -> bool:
    window = text[max(0, start - 20) : start]
    return bool(_DESTINATION_PREPOSITION_RE.search(window))


def _content_marker_spans(text: str, candidate_starts: list[int]) -> list[tuple[int, int]]:
    """(start, end) spans of dictated CONTENT introduced by a marker ("with contents:",
    "containing", "add a line", ...).

    A span runs from the marker to the first candidate after it that is EITHER:
    - destination-preposition-governed ("...contents: hello AT /tmp/foo.txt") -- that candidate
      names a real PLACE, in the same clause as the content itself, or
    - past a genuine sentence break AND governed by its own write-intent verb ("...containing:
      old logs. Now CREATE /tmp/x") -- a fresh instruction, not more of the dictated content,

    or to the end of the text if neither follows. Multi-line dictated content (a script, a config
    file, a note) has no other natural terminator inside the turn; truncating at the next newline
    or sentence is what silently re-opened D16 for line 2 onward (ANVIL R1).
    """
    sentence_boundaries = [match.end() for match in _SENTENCE_BOUNDARY_RE.finditer(text)]
    spans: list[tuple[int, int]] = []
    for marker in _CONTENT_MARKER_RE.finditer(text):
        span_start = marker.end()
        span_end = len(text)
        for candidate_start in candidate_starts:
            if candidate_start < span_start:
                continue
            past_a_sentence_break = any(
                span_start <= boundary <= candidate_start for boundary in sentence_boundaries
            )
            if _is_destination_preposition_governed(text, candidate_start) or (
                past_a_sentence_break and _write_intent_precedes(text, candidate_start)
            ):
                span_end = candidate_start
                break
        spans.append((span_start, span_end))
    return spans


def _is_content_marker_governed(start: int, content_spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start < span_end for span_start, span_end in content_spans)


def _is_payload_span(
    text: str,
    start: int,
    *,
    fence_spans: list[tuple[int, int]],
    content_spans: list[tuple[int, int]],
) -> bool:
    """Whether the rooted candidate at `start` is dictated CONTENT rather than an addressed
    destination. Judged from the candidate's own local context (its line, its fence, its content
    span) -- never by truncating the scan at some earlier point in the turn, so a real destination
    named later is never silently discarded just because payload appeared first.
    """
    return (
        _is_shebang_adjacent(text, start)
        or _is_inside_fence(start, fence_spans)
        or _is_content_marker_governed(start, content_spans)
    )


# A full local path the user put in quotes or backticks: `"C:\Users\me\VOOL Test\notes"`. The rooted
# path pattern above never starts right after a quote, so a quoted path, spaces and all, was no
# candidate at all. The span runs to the matching quote on the same line.
_QUOTED_ROOTED_PATH_RE = re.compile(
    r"(?P<quote>[\"'`])(?P<path>(?:~[/\\]|[A-Za-z]:[\\/]|/(?![/\\]))[^\"'`\r\n<>|]*?)(?P=quote)"
)
# How many space-separated words an unquoted path may be extended through (`_extend_through_spaces`).
_MAX_SPACED_WORDS = 16
_NEXT_WORD_RE = re.compile(r"[ \t]+[^\s`'\"<>|]+")
_ON_WINDOWS = os.name == "nt"


def _path_separators() -> str:
    return "\\/" if os.name == "nt" else "/"


def _spaces_may_extend(candidate: str) -> bool:
    """Only a full local path is extended, and only by looking at what exists: never a share, a
    device, a root-relative path or a mapped network drive on Windows (looking a share up is already
    the SMB connection), and never a ``~`` path."""
    if (
        not candidate
        or windows_path_policy.refused_form(candidate)
        or windows_path_policy.refused_doubled_drive(candidate)
    ):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", candidate):
        # A mapped network drive is a share under a letter: looking it up is the SMB connection.
        # GetDriveTypeW does no I/O on the share; only a drive Windows calls local is looked at.
        return not _ON_WINDOWS or windows_known_folders._drive_is_remote(candidate[:2]) is False
    return os.name != "nt" and candidate.startswith("/") and not candidate.startswith(("//", "/\\"))


def _spaced_component_state(path: str) -> str:
    """For ``path``, judged at the component holding its LAST space: ``"exists"`` when that whole
    component is an existing folder (or, at the end of ``path``, an existing file), ``"prefix"``
    when it is still open at the end of ``path`` and begins the name of an entry in its folder, and
    ``""`` otherwise."""
    seps = _path_separators()
    space = max(path.rfind(" "), path.rfind("\t"))
    if space < 0:
        return ""
    head = max(path.rfind(sep, 0, space) for sep in seps)
    if head < 0:
        return ""
    tails = [found for found in (path.find(sep, space) for sep in seps) if found >= 0]
    tail = min(tails) if tails else -1
    parent, name = path[: head + 1], path[head + 1 : tail if tail >= 0 else len(path)]
    if not name.strip():
        return ""
    try:
        if tail >= 0:
            return "exists" if os.path.isdir(parent + name) else ""
        if os.path.exists(parent + name):
            return "exists"
        wanted = os.path.normcase(name)
        with os.scandir(parent) as entries:
            if any(os.path.normcase(entry.name).startswith(wanted) for entry in entries):
                return "prefix"
    except (OSError, ValueError):
        return ""
    return ""


def _marked_spellings() -> list[str]:
    """On Windows, every spelling of a marked folder: as the owner listed it (the variable and the
    owner's own list) and as the gate resolved it (final path, case-folded). Off Windows, or when none
    can be read, none. A spelling naming a share or a device is left out."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return []
    spellings: list[str] = []
    with contextlib.suppress(Exception):
        spellings.extend(str(root) for root in windows_write_gate.flagged_roots())
    with contextlib.suppress(Exception):
        spellings.extend(os.environ.get(windows_write_gate.ENV_VAR, "").split(os.pathsep))
    with contextlib.suppress(Exception):
        spellings.extend(str(folder) for folder in windows_write_gate._store_start_folders())
    kept: list[str] = []
    for spelling in spellings:
        spelling = str(spelling or "").strip().rstrip("\\/")
        if len(spelling) >= 3 and not windows_path_policy.refused_form(spelling) and spelling not in kept:
            kept.append(spelling)
    return kept


def _fold(text: str) -> str:
    """Case-folded with either separator as `\\`, one character for one, so positions still match."""
    return "".join(low if len(low := char.lower()) == 1 else char for char in text.replace("/", "\\"))


# After a marked folder: a separator, a space, a quote, or sentence punctuation that ends the word.
# "<marked>.backup" is another folder, so a `.` followed by more of a name does not end it.
_MARKED_FOLDER_END_RE = re.compile(r"[\\/\s\"'`]|[.,;:!?)]+(?=[\s\"'`]|$)")
_REST_OF_WORD_RE = re.compile(r"[^\s`'\"<>|]*")


def _marked_folder_typed_at(text: str, start: int) -> int:
    """Where the path typed at ``start`` ends when it begins with a marked folder named whole, or
    ``start`` when it does not.

    Compared as text, case-folded and with either separator, against the folders the owner marked: no
    lookup at all, so it does not depend on the drive type, on what the disk walk can read, or on how
    the file system folds the case of `Ä`. Windows CI at 396445b1 answered "create hello.txt in
    <tmp>\\VOOL Test\\Allowed Ä folder" with "outside every folder" while Linux passed. The folder
    must end at a separator, a space, a quote or sentence punctuation, never inside a longer name; the
    rest of that word (`\\hello.txt`) is part of the path."""
    best = start
    folded = _fold(text)
    for spelling in _marked_spellings():
        wanted = _fold(spelling)
        stop = start + len(wanted)
        if folded[start:stop] != wanted:
            continue
        if stop < len(text) and not _MARKED_FOLDER_END_RE.match(text, stop):
            continue
        if stop < len(text) and text[stop] in "\\/":
            stop = _REST_OF_WORD_RE.match(text, stop).end()
        best = max(best, stop)
    return best


def _extend_through_spaces(text: str, start: int, end: int) -> int:
    """Where an unquoted path typed at ``start`` really ends, when it names a folder with spaces.

    Captest B3, 2026-11-08: "create hello.txt in C:\\Users\\VOOLT\\VOOL Test\\Allowed Ä folder with the
    text hi" was read as `C:\\Users\\VOOLT\\VOOL`. The path is extended one word at a time while each
    spaced component it gains is a folder or file that exists (or, while still being read, starts
    the name of one), and it ends after the longest extension that named something on disk. A word
    that would only make up a new name ("... folder with") is never taken in."""
    # A marked folder typed whole is read whole first, without a lookup (`_marked_folder_typed_at`).
    end = max(end, _marked_folder_typed_at(text, start))
    if not _spaces_may_extend(_clean_path(text[start:end])):
        return end
    best, pos = end, end
    for _ in range(_MAX_SPACED_WORDS):
        word = _NEXT_WORD_RE.match(text, pos)
        if word is None:
            break
        pos = word.end()
        state = _spaced_component_state(_clean_path(text[start:pos]))
        if state == "exists":
            best = pos
        elif state != "prefix":
            break
    return best


def _rooted_candidates(body: str, *, typed_shares: bool = False, look_up: bool = True) -> list[tuple[int, str]]:
    """``(start, raw path)`` for every rooted path in ``body``, quoted or not, in order. A URL's
    authority section is never one. With ``typed_shares``, a share, device or NT path typed with
    backslashes is a candidate too, quoted or not (`_TYPED_SHARE_RE`), taken as typed (never
    extended through spaces). Without ``look_up``, no unquoted path is extended through spaces either,
    so nothing on disk is looked at."""
    found: list[tuple[int, str]] = []
    quoted: list[tuple[int, int]] = []
    for match in _QUOTED_ROOTED_PATH_RE.finditer(body):
        found.append((match.start("path"), match.group("path")))
        quoted.append((match.start("path"), match.end("path")))
    for match in _ROOTED_PATH_RE.finditer(body):
        start = match.start()
        if _is_url_authority(body, start) or any(low <= start < high for low, high in quoted):
            continue
        end = _extend_through_spaces(body, start, match.end()) if look_up else match.end()
        found.append((start, body[start:end]))
    if typed_shares:
        starts = {start for start, _raw in found}
        found.extend(
            (match.start(), match.group("path"))
            for match in _TYPED_SHARE_RE.finditer(body)
            if match.start() not in starts and not _is_url_authority(body, match.start())
        )
    return sorted(found)


def _destination_candidates(
    body: str, *, typed_shares: bool = False, look_up: bool = True
) -> Iterator[tuple[int, str]]:
    """``(start, path)`` for every rooted candidate in ``body`` that is not payload, in order. With
    ``typed_shares``, a share, device or NT path typed with backslashes is a candidate too, quoted or
    not (`_TYPED_SHARE_RE`)."""
    fence_spans = _fenced_spans(body)
    candidate_matches = _rooted_candidates(body, typed_shares=typed_shares, look_up=look_up)
    content_spans = _content_marker_spans(body, [start for start, _raw in candidate_matches])
    for start, raw in candidate_matches:
        candidate = _clean_path(raw)
        if len(candidate) <= 1:
            continue
        if _is_payload_span(body, start, fence_spans=fence_spans, content_spans=content_spans):
            continue
        yield start, candidate


def write_destinations(text: str) -> list[str]:
    """The full local paths this turn asks us to write, in order: rooted, with a destination verb
    just before them, and not dictated content. A quoted path, and an unquoted one through folders
    with spaces in their names, is read whole. Whether a path may be written is not decided here:
    that is the write gate's."""
    body = str(text or "")
    found: list[str] = []
    for start, candidate in _destination_candidates(body):
        if typed_path_kind(body, start, candidate) or not _write_intent_precedes(body, start):
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def named_full_paths(text: str) -> list[str]:
    """Every full local path ``text`` names, in order, read as `write_destinations` reads them (quoted,
    or through folders with spaces) but with no verb needed: a move names its source and destination
    after one verb. Shares, devices and paths in dictated content are left out."""
    body = str(text or "")
    found: list[str] = []
    for start, candidate in _destination_candidates(body):
        if typed_path_kind(body, start, candidate):
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def unreachable_write_target(text: str, *, source_context: dict[str, object] | None) -> str:
    """The rooted path this turn asks us to write and cannot reach, or ``""``.

    Returns the FIRST such path, because that is the one the reply has to be about. Every rooted
    candidate is classified on its own local context (`_is_payload_span`) rather than the scan
    being cut off at the first guessed content marker -- a shebang or code fence anywhere in the
    turn marks only ITS OWN candidate as payload, never the ones around it. On Windows a candidate
    naming a share, a device or no full local folder is unreachable without being resolved.
    """
    body = str(text or "")
    if not body.strip():
        return ""
    roots = allowed_write_roots(source_context)
    for start, candidate in _destination_candidates(body):
        if not typed_path_kind(body, start, candidate) and _is_reachable(candidate, roots):
            continue
        if _write_intent_precedes(body, start):
            return candidate
    return ""


def non_local_write_target(text: str, *, network_only: bool = False, look_up: bool = True) -> tuple[str, str]:
    """On Windows, the first path this turn asks us to write that names a share, a device or no full
    local folder (only a share or a device, with ``network_only``), with its kind; ``("", "")``
    otherwise, and always off Windows. Nothing is looked up. A share or device typed with backslashes
    (`\\\\host\\s\\x`, `\\\\?\\C:\\x`), quoted or not, is found as typed. A block comment opened
    against its text (``/*eslint-disable*/``) is not one. A path named after one this passes over is
    judged by the verb before that one too. Without ``look_up``, a local path typed through folders
    with spaces is not extended by looking at what exists (`_extend_through_spaces`): that never
    changes whether a path is a share or a device, so a ``network_only`` reading needs no lookup.
    """
    body = str(text or "")
    passed_start = -1
    for start, candidate in _destination_candidates(body, typed_shares=windows_path_policy.active(), look_up=look_up):
        if not candidate.strip("/\\*"):
            # A bare `//`, `///`, `/*` or `/**` (a code comment's marker in the text to write) names no
            # host and no folder as typed, so it is not answered here. The input normalizer can still
            # join it to a host typed after a space (`(// host/s` becomes `(//host/s`); the lanes' own
            # checks on that text answer it, as `unreachable_write_target` does for a write.
            continue
        kind = typed_path_kind(body, start, candidate)
        if network_only and kind not in windows_path_policy.NETWORK_KINDS:
            continue
        if (
            kind not in windows_path_policy.NETWORK_KINDS
            and candidate.startswith("/*")
            and not _SHARE_CAN_START_INSIDE_RE.search(candidate)
        ):
            # A block comment opened against its text (`/*eslint-disable*/`, `a{color:red}/*x*/`) is
            # root-relative only by its leading `/`, and `*` is never part of a Windows file or folder
            # name, so no lane can open it as a folder. One a share, a device or a URL can start inside
            # (`/*x*///host/s`) is answered as typed, as on base, and so is a share, device or NT path.
            passed_start = start if passed_start < 0 else passed_start
            continue
        if kind and _write_intent_governs(body, start, passed_start):
            return candidate, kind
    return "", ""


#: One word of a turn, or a quoted span, as a path a person types it.
_TYPED_WORD_RE = re.compile(r"(?P<quote>[\"'`])(?P<quoted>[^\"'`\r\n<>|]+)(?P=quote)|(?<![^\s(])(?P<word>[^\s\"'`<>|]+)")
#: A move verb just before a path: "move x.txt to ..\\y.txt" names the move's paths after it.
_MOVE_INTENT_RE = re.compile(r"\b(?:move|mv|relocate)\b", re.IGNORECASE)


def _relative_climbs(text: str) -> list[tuple[int, str]]:
    """``(start, path)`` for every relative path ``text`` names that holds a `..` name, in order:
    ``..``, ``..\\y.txt``, ``../y.txt``, ``sub\\..\\..\\y.txt``. A rooted path (a drive, ``\\``, ``/``
    or ``~``) is the rooted readers' (`write_destinations`); a word with no `..` name is left alone."""
    found: list[tuple[int, str]] = []
    seps = _path_separators()
    for match in _TYPED_WORD_RE.finditer(str(text or "")):
        group = "quoted" if match.group("quoted") is not None else "word"
        raw = match.group(group)
        path = _clean_path(raw.lstrip("("))
        if not path or path.startswith(("~", "/", "\\")) or re.match(r"^[A-Za-z]:", path) or "://" in path:
            continue  # a rooted path, or a URL
        if ".." not in re.split("[" + re.escape(seps) + "]", path):
            continue
        found.append((match.start(group), path))
    return found


def _chat_folder(source_context: dict[str, object] | None) -> str:
    """The folder a relative path is placed from: the chat's own workspace, as the workspace tools
    place it (`resolve_workspace_path`)."""
    workspace = str(
        (source_context or {}).get("workspace") or (source_context or {}).get("workspace_root") or ""
    ).strip()
    if not workspace:
        try:
            from core.runtime_paths import active_workspace_dir

            workspace = str(active_workspace_dir())
        except Exception:
            workspace = ""
    return workspace


def _placed_from(folder: str, path: str) -> str:
    """``path`` joined onto ``folder`` with its `..` names followed, by text: nothing is looked up.
    Either separator the platform reads is a separator."""
    native = path
    for sep in _path_separators():
        if sep != os.sep:
            native = native.replace(sep, os.sep)
    return os.path.normpath(os.path.join(folder, native))


def climbing_relative_target(text: str, *, source_context: dict[str, object] | None) -> tuple[str, str, str]:
    """On Windows, ``(kind, path, leads_to)`` for the first relative path with a `..` name that this
    turn asks to write (``kind`` "write") or move (``kind`` "move"), placed from the chat's own folder,
    that leads outside every marked folder; ``("", "", "")`` otherwise, and always off Windows.

    Captest round 2 (build a22771c2): in a marked folder, "move x.txt to ..\\y.txt" and "write hi to
    ..\\y.txt" were never refused as outside the marked folders. The rooted readers above take only a
    full path, so a climb typed relative to the chat's folder reached the lanes, which place a relative
    path from somewhere else (the machine writer from the home, the move tool from VOOL's working
    folder) or not at all. The path is placed by text, as the chat's own workspace tools would place it;
    a climb that stays inside a marked folder is left to the lanes and the gate, as before. This only
    refuses: it never chooses a destination."""
    from core import windows_write_gate

    body = str(text or "")
    if not windows_write_gate.active() or not body.strip():
        return "", "", ""
    climbs = _relative_climbs(body)
    if not climbs:
        return "", "", ""
    folder = _chat_folder(source_context)
    if not folder or windows_path_policy.refused_form(folder, network_only=True):
        return "", "", ""
    for start, path in climbs:
        window = body[max(0, start - _WRITE_INTENT_WINDOW) : start]
        if _MOVE_INTENT_RE.search(window) and not _USER_OWN_ACTION_RE.search(window):
            kind = "move"
        elif _write_intent_precedes(body, start):
            kind = "write"
        else:
            continue
        leads_to = _placed_from(folder, path)
        if windows_write_gate.inside_flagged(leads_to):
            continue
        return kind, path, leads_to
    return "", "", ""


def climbing_relative_response(kind: str, path: str, leads_to: str, *, source_context: dict[str, object] | None) -> str:
    """The plain refusal for `climbing_relative_target`: the move lane's sentence for a move, the
    write-root sentence for a write, each naming where the `..` leads."""
    from core import windows_write_gate

    if kind == "move":
        return (
            f"I can't move that: `{path}` (that is `{leads_to}`) is outside the folders you marked as ones "
            f"VOOL may change ({windows_write_gate.ENV_VAR}), and on Windows VOOL moves files only inside "
            "them. Nothing was moved."
        )
    return unreachable_write_response(path, source_context=source_context, leads_to=leads_to)


def _as_on_disk(root: Path | str) -> str:
    """A writable root as the owner knows it. The gate compares folders case-folded (``c:\\users\\...``);
    on Windows the final path of the folder carries its real spelling (``C:\\Users\\...``). Other
    hosts show the root unchanged (a Mac would otherwise say ``/private/var`` for ``/var``)."""
    if os.name != "nt":
        return str(root)
    try:
        return os.path.realpath(root)
    except (OSError, ValueError):
        return str(root)


def _climb_note(path: str, *, leads_to: str = "") -> str:
    """For a path that climbs with `..`, where it really leads, as one sentence; "" otherwise.

    Captest B6 (2026-11-09): a write that used `..` to leave a marked folder wrote nothing, but the
    answer did not say that the `..` was why. A relative path is placed from the chat's folder by the
    caller (``leads_to``), never from VOOL's own working folder."""
    if ".." not in re.split(r"[\\/]", str(path or "")):
        return ""
    if leads_to:
        return (
            f"Its `..` climbs out of this chat's folder, so it leads to `{leads_to}`, which is outside every "
            "folder you marked."
        )
    try:
        leads_to = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return "Its `..` climbs out of the folder named before it, to a place I can't place exactly."
    return f"Its `..` climbs out of the folder named before it, so it leads to `{leads_to}`, which is outside every folder you marked."


def unreachable_write_response(path: str, *, source_context: dict[str, object] | None, leads_to: str = "") -> str:
    """The honest answer: name the path we were given, the roots we have, and the write we skipped."""
    roots = allowed_write_roots(source_context)
    from core import windows_write_gate

    if windows_write_gate.active():
        if not roots:
            return (
                f"I can't write to `{path}`. On Windows I only change files inside folders you mark as "
                "disposable, and no folder I can write to is marked yet. Mark one in Settings → "
                "Privacy & Permissions → Folders VOOL may change, then restart VOOL. Nothing was "
                "written on this turn."
            )
        listed = ", ".join(f"`{_as_on_disk(root)}`" for root in roots)
        climb = _climb_note(path, leads_to=leads_to)
        if climb:
            return (
                f"I can't write to `{path}`. {climb} On Windows I change files only inside the folders "
                f"you marked: {listed}. Nothing was written on this turn."
            )
        return (
            f"I can't write to `{path}` -- that path is outside every folder this build can write to, "
            f"and I'm not going to put the file somewhere else and report it as done. On Windows I can "
            f"write only under {listed}. Give me a path under one of those and I'll create it there. "
            "Nothing was written on this turn."
        )
    workspace = str(roots[0]) if roots else ""
    where = f"the workspace (`{workspace}`)" if workspace else "the active workspace"
    return (
        f"I can't write to `{path}` -- that path is outside every folder this build can write to, "
        f"and I'm not going to put the file somewhere else and report it as done. I can write under "
        f"{where}, or under `~/Desktop`, `~/Downloads` and `~/Documents`. Give me a path under one "
        f"of those and I'll create it there. Nothing was written on this turn."
    )


__all__ = [
    "allowed_write_roots",
    "climbing_relative_response",
    "climbing_relative_target",
    "named_full_paths",
    "non_local_write_target",
    "requested_rooted_paths",
    "typed_path_kind",
    "unreachable_write_response",
    "unreachable_write_target",
    "write_destinations",
]
