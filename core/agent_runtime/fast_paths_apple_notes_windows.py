"""Windows: a request to use Apple Notes gets the Mac-only answer, with no model call.

Apple Notes runs through `/usr/bin/osascript`, which Windows does not have (and which `core.posix_tools`
never lets VOOL stat or run there). The operator parser claims some Notes phrasings ("create an
Apple note ...", "list my Apple notes") and those already answer plainly. Others ("make an Apple note
called test") reached the model, which has no Notes tool on Windows. Measured on Windows 11, 11 Nov 2026:
a malformed tool call, provider timeouts, and after 263 s "failed before producing a usable answer".

This lane answers, on Windows only, a message that asks to do something with Apple Notes and that the
operator parser does NOT claim, in one fixed sentence and without running anything. A phrasing the
parser claims keeps its own lane, which already answers plainly on Windows (the bridge reports Apple
Notes as a Mac app and runs nothing). A message that only mentions Apple Notes without a request verb
("what is Apple Notes?") is left alone. Off Windows it returns None at its first statement, so macOS
and Linux are unchanged.
"""
from __future__ import annotations

import re
from typing import Any

from core import posix_tools

WINDOWS_APPLE_NOTES_ANSWER = (
    "Apple Notes is a Mac app and isn't available on Windows, so nothing was sent to Notes. "
    'I can save a note in VOOL instead: say save a note titled "Agenda" with: your text.'
)

_VERBS = (
    r"make|create|add|save|write|new|start|jot|put|store|take|list|show|open|read|append|rename|"
    r"delete|remove|edit|update|change|find|search|look|get|move|copy|send|export|sync"
)
_APPLE_NOTES = r"apple[\s-]*notes?\b"
_REQUEST_RE = re.compile(
    rf"\b(?:{_VERBS})\b[^\n.!?]*\b{_APPLE_NOTES}|\b{_APPLE_NOTES}[^\n.!?]*\b(?:{_VERBS})\b",
    re.IGNORECASE,
)


def asks_for_apple_notes(text: str) -> bool:
    """Whether the message asks to do something with Apple Notes. Pure; the same on every host."""
    return bool(_REQUEST_RE.search(str(text or "")))


def maybe_refuse_apple_notes_on_windows(
    agent: Any,
    user_input: str,
    *,
    session_id: str,
    source_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """On Windows, the fixed Mac-only answer for an Apple Notes request; None otherwise."""
    if not posix_tools.active():
        return None
    if not asks_for_apple_notes(user_input):
        return None
    from core.operator.parser import parse_operator_action_intent

    if parse_operator_action_intent(str(user_input or "")) is not None:
        return None  # the operator lane owns this phrasing and answers it on Windows itself
    return agent._fast_path_result(
        session_id=session_id,
        user_input=user_input,
        response=WINDOWS_APPLE_NOTES_ANSWER,
        confidence=0.9,
        source_context=source_context,
        reason="apple_notes_unavailable_on_windows",
    )


__all__ = ["WINDOWS_APPLE_NOTES_ANSWER", "asks_for_apple_notes", "maybe_refuse_apple_notes_on_windows"]
