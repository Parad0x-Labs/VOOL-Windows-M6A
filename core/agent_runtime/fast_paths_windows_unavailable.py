"""Windows: a request for Apple Calendar, the Skill Codex or VOOL Drop gets a fixed answer, with no model call.

None of the three runs on Windows in 0.7.2. Since 0.7.3 Drop runs on Windows from its own command line,
never from chat, so its answer says that instead. Apple Calendar is EventKit, which needs macOS (Settings and
core/operator/calendar_accounts.py already refuse it there). The Skill Codex's signer runs on a Mac, and VOOL
Drop refuses on Windows before it touches anything (core/drop require_enabled). In chat, though, the model
had no tool for them and answered in prose. Measured on Steve, 14 Nov 2026 (0.7.2 at 0360e41e): "connect my
Apple Calendar account" got "I'll help you connect your Apple Calendar", "open the Skill Codex and install a
skill" got "Skill Codex opened" over the built-in skill list, and "send this file to my phone with VOOL Drop"
got an offer to help use Drop.

This lane answers, on Windows only, a message that asks to do something with one of them, in one fixed
sentence and without running anything. A message that only names one without a request verb ("what is the
Skill Codex?") is left alone, and so is an iCloud or Google calendar, which Windows can add. The operator
parser keeps the phrasings it claims (its calendar lane answers on Windows itself). Off Windows it returns
None at its first statement, so macOS and Linux are unchanged.
"""
from __future__ import annotations

import re
from typing import Any

from core import posix_tools

WINDOWS_APPLE_CALENDAR_ANSWER = (
    "Apple Calendar on this computer needs macOS, so nothing was connected. On Windows, add iCloud as a CalDAV "
    "account in Settings > Calendars (server address and an app-specific password), or add a Google calendar there."
)
WINDOWS_SKILL_CODEX_ANSWER = (
    "The Skill Codex isn't available on Windows in this release, so nothing was opened or installed. "
    "VOOL's built-in skills still work here."
)
# Drop runs on Windows since 0.7.3 (core/drop/service.py, owner-only ACLs, marked folders only), but only
# from its own controller command line (python -m core.drop) with VOOL_DROP=1: no chat tool reaches it on
# any OS. So the chat answer says that, instead of the 0.7.2 "isn't available on Windows", which the
# 0.7.3 note contradicts (Steve row 10, 23 Nov 2026).
WINDOWS_DROP_ANSWER = (
    "VOOL Drop doesn't run from chat. It's an off-by-default command-line preview, so nothing was sent or received."
)

_VERBS = (
    r"connect|link|add|set\s*up|setup|sync|use|open|show|list|read|get|check|browse|search|find|install|"
    r"download|publish|upload|send|share|receive|transfer|move|copy|start|turn\s+on|enable|launch|run"
)


# Within one sentence. A `.` ends it only before a space or the end, so a file or path typed between
# the verb and the subject ("send the file C:\\...\\drop-test.txt to my phone with VOOL Drop") does
# not split the sentence (Steve row 10, 23 Nov 2026).
_SAME_SENTENCE = r"(?:[^\n.!?]|\.(?=\S))*"


def _request(subject: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b(?:{_VERBS})\b{_SAME_SENTENCE}{subject}|{subject}{_SAME_SENTENCE}\b(?:{_VERBS})\b",
        re.IGNORECASE,
    )


_LANES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (_request(r"\bapple[\s-]*calendars?\b"), WINDOWS_APPLE_CALENDAR_ANSWER, "apple_calendar_unavailable_on_windows"),
    (_request(r"\bskill[\s-]*codex\b"), WINDOWS_SKILL_CODEX_ANSWER, "skill_codex_unavailable_on_windows"),
    (_request(r"\bvool[\s-]*drop\b"), WINDOWS_DROP_ANSWER, "drop_unavailable_on_windows"),
    # "drop C:\\...\\file.txt": the message opens with "drop" and a full Windows path (drive or share). Steve row 10
    # (0.7.3 at 8987cac7): it reached the model, which answered "Got it — dropping ..." with nothing run. Only that
    # opening is claimed: "drop the last paragraph" or a path later in the sentence is not.
    (re.compile(r"""^\s*drop\s+(?:the\s+)?(?:file\s+)?["'`]?(?:[A-Za-z]:[\\/]|\\\\)""", re.IGNORECASE),
     WINDOWS_DROP_ANSWER, "drop_unavailable_on_windows"),
)


def windows_unavailable_answer(text: str) -> tuple[str, str] | None:
    """(answer, reason) for a message that asks to use one of the three; None otherwise. Pure; the same on
    every host."""
    message = str(text or "")
    for pattern, answer, reason in _LANES:
        if pattern.search(message):
            return answer, reason
    return None


def maybe_refuse_windows_unavailable(
    agent: Any,
    user_input: str,
    *,
    session_id: str,
    source_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """On Windows, the fixed answer for an Apple Calendar, Skill Codex or VOOL Drop request; None otherwise."""
    if not posix_tools.active():
        return None
    found = windows_unavailable_answer(user_input)
    if found is None:
        return None
    from core.operator.parser import parse_operator_action_intent

    if parse_operator_action_intent(str(user_input or "")) is not None:
        return None  # the operator lane owns this phrasing and answers it on Windows itself
    answer, reason = found
    return agent._fast_path_result(
        session_id=session_id,
        user_input=user_input,
        response=answer,
        confidence=0.9,
        source_context=source_context,
        reason=reason,
    )


__all__ = [
    "WINDOWS_APPLE_CALENDAR_ANSWER",
    "WINDOWS_DROP_ANSWER",
    "WINDOWS_SKILL_CODEX_ANSWER",
    "maybe_refuse_windows_unavailable",
    "windows_unavailable_answer",
]
