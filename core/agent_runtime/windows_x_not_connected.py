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
"""
from __future__ import annotations

from typing import Any

X_NOT_CONNECTED = (
    "X is not connected. Add your X keys in Settings > API Keys > X (Twitter) > Connect X."
)
X_NOT_CONNECTED_LOCAL_ONLY = (
    "Local Only is on, so VOOL stays on this computer and does not reach X. "
    "X is also not connected: add your X keys in Settings > API Keys > X (Twitter) > Connect X."
)


def _x_demanded(text: str, source_context: dict[str, Any] | None) -> bool:
    from core.retrieval_constraints import analyze_retrieval_constraints
    from core.tool_demand_signals import resolve_demand_signals
    from core.turn_prohibitions import conserve_retrieval_constraints

    if "x" not in resolve_demand_signals(text).required_families:
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
    try:
        from core.x_api import connected

        if connected() or not _x_demanded(text, source_context):
            return None
    except Exception:
        return None
    try:
        from core.remote_fetch_policy import local_only_active

        local_only = bool(local_only_active(source_context))
    except Exception:
        local_only = False
    return X_NOT_CONNECTED_LOCAL_ONLY if local_only else X_NOT_CONNECTED
