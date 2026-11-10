"""Drive a `machine.move_path` the way the owner approves one: its card, the Allow, then the approved call.

On Windows the owner's Allow on the card is the move's consent, and any other call (a bare dispatch, Auto,
a task grant) is refused before anything moves (captest R4-1b, tests/test_windows_machine_move_allow_is_consent.py).
A row that means "this move is allowed and lands" therefore asks for the card, answers Allow on it, and sends
the call again with that live token, through the authorized boundary every approved tool call crosses. That
boundary dispatches through the real `execute_runtime_tool`, so every containment check still runs. Off Windows
the same route asks the move's live OS confirmation as before, which the rows answer through
`os_consent_gate`'s test hook.
"""
from __future__ import annotations

import uuid
from typing import Any


def move_allowed_on_its_card(arguments: dict[str, Any], *, source_context: dict[str, Any]) -> Any:
    """Raise the move's card, Allow it, and run the call that spends that Allow. Returns the move's result."""
    from core.authorized_tool_execution import execute_authorized_runtime_tool
    from core.mode_permission_policy import resolve_approval

    run = uuid.uuid4().hex[:10]
    task_id = f"move-card-{run}"
    context = {"session_id": f"move-card-{run}", "operating_mode": "manual", **dict(source_context or {})}
    card = execute_authorized_runtime_tool(
        "machine.move_path", dict(arguments), task_id=task_id, source_context=context
    )
    assert card is not None and card.status == "pending_approval", (
        f"the move raised no card: {getattr(card, 'status', None)}: {getattr(card, 'response_text', '')}"
    )
    request = dict((card.details or {}).get("approval_request") or {})
    assert request.get("intent") == "machine.move_path", request
    token = str(request.get("approval_id") or "")
    assert token and resolve_approval(token, decision="allow") is not None, request
    return execute_authorized_runtime_tool(
        "machine.move_path",
        dict(arguments),
        task_id=task_id,
        source_context={**context, "mode_approval_token": token},
    )
