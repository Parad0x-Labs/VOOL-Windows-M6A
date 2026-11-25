"""Windows: the person's answer to a wallet "which one?" proposes the contact they named.

Steve, row 38 (0.7.3 at 9e1eb4b4, 24 Nov 2026): "send Testy 1 USDT" with two saved contacts, Testy and Testy R072, asked
which one, once. The person answered "Testy R072" in the same chat, and the model replied "Got it — Testy R072. (Nothing
ran on this turn: no tool was called.)": no proposal and no card, so a send to a contact whose name clashes could never
finish. When the chat's last turn ended on that question (core/runtime_execution_tools.py
_remember_pending_recipient_choice), a message that names exactly ONE of the offered contacts, by its full name
(case-insensitive, whole words, never a prefix) or one of its full addresses, runs wallet.propose with the original
request and that contact's id. The proposal waits on its approval card like any other; nothing is signed or sent here,
and a frozen wallet answers with the freeze. A message naming none or several of them, or asking anything more, is
handled as before. Off Windows this lane never answers.
"""

from __future__ import annotations

import re
from typing import Any

#: Whole words around a name or address: not inside a longer word ("Testy" is not in "Testyx").
_EDGE = r"(?<![\w-])"
_EDGE_END = r"(?![\w-])"
#: What may surround the pick: only these filler words, never anything else. A word not on the list (no, not, don't,
#: cancel, stop, wait, other, wrong, a new name...) means the message is more than a pick and proposes nothing (Chief of
#: Staff, 24 Nov: "no, cancel Testy R072" must never propose). A digit would be a new amount, so it is refused too.
_FILLER_WORDS = frozenset({
    "the", "to", "it", "send", "pay", "please", "yes", "ok", "okay", "one", "contact", "that", "this", "him", "her",
    "them", "i", "mean", "a",
})
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_PUNCTUATION_ONLY_RE = re.compile(r"[\s.,!:;\-—–()\"'’“”]*")  # no "?": a question is not a pick


def _spans(text: str, needle: str, *, fold: bool) -> list[tuple[int, int]]:
    needle = needle.strip()
    if not needle:
        return []
    pattern = _EDGE + r"\s+".join(re.escape(part) for part in needle.split()) + _EDGE_END
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, re.IGNORECASE if fold else 0)]


def picked_candidate(text: str, candidates: tuple[dict[str, str], ...]) -> dict[str, str] | None:
    """The one offered contact `text` names, or None when it names none, several, or says more than a pick."""
    message = str(text or "").strip()
    if not message or len(message) > 200:
        return None
    hits: list[tuple[int, int, str]] = []
    by_address: dict[str, set[str]] = {}
    for candidate in candidates:
        contact_id = str(candidate.get("contact_id") or "")
        for start, end in _spans(message, str(candidate.get("display_name") or ""), fold=True):
            hits.append((start, end, contact_id))
        values = str(candidate.get("values") or "").split("\x1f")
        endpoint_ids = str(candidate.get("endpoint_ids") or "").split("\x1f")
        for index, value in enumerate(values):
            fold = value.lower().startswith("0x")  # an EVM address in any case; a base58 one exactly as saved
            for start, end in _spans(message, value, fold=fold):
                hits.append((start, end, contact_id))
                by_address.setdefault(contact_id, set()).add(endpoint_ids[index] if index < len(endpoint_ids) else "")
    # A name inside a longer offered name it is part of ("Testy" in "Testy R072") is that longer name, not a second pick.
    kept = [hit for hit in hits if not any(o[0] <= hit[0] and hit[1] <= o[1] and (o[1] - o[0]) > (hit[1] - hit[0])
                                           for o in hits)]
    picked = {contact_id for _s, _e, contact_id in kept}
    if len(picked) != 1:
        return None
    rest = message
    for start, end, _c in sorted(kept, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    words = _WORD_RE.findall(rest)
    if any(word.casefold() not in _FILLER_WORDS for word in words):
        return None  # fail closed: a refusal, a negation, a number or any other word is not a pick
    if not _PUNCTUATION_ONLY_RE.fullmatch(_WORD_RE.sub(" ", rest)):
        return None  # a symbol that is not plain punctuation ("$", "@", "/") is not a pick either
    (contact_id,) = picked
    candidate = dict(next(c for c in candidates if str(c.get("contact_id") or "") == contact_id))
    # A pick by one of its full addresses names that endpoint, so a contact with two addresses is not asked again.
    endpoints = by_address.get(contact_id, set())
    candidate["destination"] = next(iter(endpoints)) if len(endpoints) == 1 and all(endpoints) else contact_id
    return candidate


def maybe_handle_contact_pick(
    agent: Any,
    user_input: str,
    *,
    session_id: str,
    source_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """On Windows, the proposal for the contact the person picked after a wallet "which one?"; None otherwise."""
    from core import windows_write_gate

    if not windows_write_gate.active():
        return None
    from core import runtime_execution_tools as tools

    context = dict(source_context or {})
    context.setdefault("session_id", session_id)
    pending = tools.take_pending_recipient_choice(context)
    if pending is None:
        return None
    request, candidates = pending
    candidate = picked_candidate(user_input, candidates)
    if candidate is None:
        return None
    arguments = {**request, "destination": str(candidate.get("destination") or candidate["contact_id"])}
    execution = tools.execute_runtime_tool("wallet.propose", arguments, source_context=context)
    if execution is None:
        return None
    response = str(execution.response_text or "").strip()
    proposal_id = str(dict(dict(execution.details or {}).get("proposal") or {}).get("proposal_id") or "")
    if execution.ok and proposal_id:
        from core.wallet import transfers

        response = transfers.pending_approval_reply(proposal_id) or response
    return agent._fast_path_result(
        session_id=session_id,
        user_input=user_input,
        response=response or "Nothing was proposed, signed or sent.",
        confidence=0.9,
        source_context=source_context,
        reason="windows_contact_pick",
        runtime_event_details={"tool": "wallet.propose", "status": str(execution.status or ""),
                               "proposal_id": proposal_id},
    )


__all__ = ["maybe_handle_contact_pick", "picked_candidate"]
