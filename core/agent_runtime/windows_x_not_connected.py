"""Windows: an X ask with no X keys is answered by VOOL itself, without a model round.

Measured on Steve (r072-4, session openclaw:bd5f9a067a907d9088fd): "read my latest posts on X" seats
x.user_posts, but with X not connected the X contract is unsupported, so the turn fell through to the
web lane. With Web lookups on the model was handed web.search for a question only the X tools can
answer; with Web lookups off (or Local Only on) the reply was "Web lookups are off", which names a
switch that would not have helped. Mac keeps that design (tests/test_x_turn_routing.py: nothing
changes while X is not connected), so this answers only behind the Windows seam.

The words name what the Settings page actually shows: the API Keys group, its "X (Twitter)" row and
that row's "Connect X" button. With Local Only on, the reply names Local Only first, since that is
what keeps VOOL off X even once the keys are added.

A publish or schedule ask is not answered here: publishing does not exist in the X lane
(core.x_editorial), so adding keys would not make it possible and the line would point at a switch
that does not help. That turn goes on as before and the answer says VOOL cannot publish
(tests/test_x_editorial_served.py::test_unsupported_publish_request_returns_typed_guidance).

Nor is a public X search ("check Toly on X", seating only x.search) while Web lookups are on and
Local Only is off: the web lane answers who someone is from public pages, as on Mac
(tests/test_alpha_semantic_context_smoke.py). With Web lookups off or Local Only on it gets the line,
since there is no web lane to answer it.
"""
from __future__ import annotations

import re
from typing import Any

X_NOT_CONNECTED = (
    "X is not connected. Add your X keys in Settings → API Keys → X (Twitter) → Connect X."
)
X_NOT_CONNECTED_LOCAL_ONLY = (
    "Local Only is on, so VOOL does not reach X. "
    "X is also not connected: add your X keys in Settings → API Keys → X (Twitter) → Connect X."
)

# Publishing and scheduling, which no X key enables. Posting, replying and messaging are not here:
# with X connected those are prepared for the user's approval, so the keys are the honest answer.
_PUBLISH_OR_SCHEDULE = re.compile(r"\b(?:publish|schedul)", re.IGNORECASE)
# A public X search, which the web lane can answer from public pages while Web lookups are on.
_PUBLIC_SEARCH_INTENTS = frozenset({"x.search"})


def _x_demanded(text: str, source_context: dict[str, Any] | None, *, web_answers_search: bool) -> bool:
    from core.retrieval_constraints import analyze_retrieval_constraints
    from core.tool_demand_signals import resolve_demand_signals
    from core.turn_prohibitions import conserve_retrieval_constraints

    signals = resolve_demand_signals(text)
    if "x" not in signals.required_families:
        return False
    x_intents = {intent for intent in signals.explicit_intents if intent.startswith("x.")}
    if web_answers_search and x_intents and x_intents <= _PUBLIC_SEARCH_INTENTS:
        return False
    constraints = conserve_retrieval_constraints(analyze_retrieval_constraints(text), source_context)
    return not constraints.forbids_all_tools and not constraints.forbids("x")


def maybe_handle_x_not_connected(
    user_input: str, *, source_context: dict[str, Any] | None = None
) -> str | None:
    """The fixed reply for an X ask while X is not connected, on Windows. None off Windows, when X
    is connected, or when the message does not ask for X."""
    from core import windows_write_gate

    text = str(user_input or "")
    if not windows_write_gate.active() or not text.strip():
        return None
    if _PUBLISH_OR_SCHEDULE.search(text):
        return None
    try:
        from core.remote_fetch_policy import local_only_active

        local_only = bool(local_only_active(source_context))
    except Exception:
        local_only = False
    try:
        from core.policy_engine import web_lookups_allowed

        web_on = bool(web_lookups_allowed())
    except Exception:
        web_on = False
    try:
        from core.x_api import connected

        if connected() or not _x_demanded(text, source_context, web_answers_search=web_on and not local_only):
            return None
    except Exception:
        return None
    return X_NOT_CONNECTED_LOCAL_ONLY if local_only else X_NOT_CONNECTED
