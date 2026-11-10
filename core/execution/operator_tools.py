from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.execution.models import ToolIntentExecution, _tool_observation
from core.local_operator_actions import OperatorActionIntent, dispatch_operator_action
from core.operator.models import operator_step_executed
from core.tool_argument_aliases import side_effect_class_for_intent


def execute_operator_tool(
    intent: str,
    arguments: dict[str, Any],
    *,
    task_id: str,
    session_id: str,
    dispatch_operator_action_fn=dispatch_operator_action,
    source_context: dict[str, Any] | None = None,
) -> ToolIntentExecution:
    operator_kind = intent.split(".", 1)[1]
    operator_intent = build_operator_action_intent(operator_kind, arguments)
    dispatch = dispatch_operator_action_fn(
        operator_intent,
        task_id=task_id,
        session_id=session_id,
    )
    approved = False
    if intent in _APPROVED_PREVIEW_INTENTS:
        from core.execution_gate import claim_operator_approved_call

        approved = claim_operator_approved_call(_APPROVED_PREVIEW_INTENTS)
        if approved:
            dispatch = _run_approved_preview(
                operator_intent, dispatch, task_id=task_id, session_id=session_id,
                dispatch_operator_action_fn=dispatch_operator_action_fn,
            )
    # The turn-keyed execution TRUTH: an operator action that ran is a tool that ran, so
    # the signed honesty ledger and the completion-claim guards must read it from the one
    # store built for that question. Without this fact a mixed-demand turn whose
    # note/calendar unit really executed still answers with the honest-looking "cannot
    # verify" replacement, because no other path records the operator lane's execution
    # against the turn. Best-effort, never raises, and no fact without a real turn
    # identity (resolve_turn_key refuses to invent one).
    try:
        from core.execution_truth import KIND_TOOL, record_execution, resolve_turn_key

        turn_key = resolve_turn_key(dict(source_context or {}), None)
        if turn_key:
            record_execution(
                session_id=str(session_id or ""),
                turn_key=turn_key,
                kind=KIND_TOOL,
                name=str(intent or ""),
                ok=operator_step_executed(dispatch),
                status=str(dispatch.status or ""),
                dedupe=f"{intent}:{dispatch.status}",
            )
    except Exception:
        pass
    if dispatch.status == "reported" and side_effect_class_for_intent(intent) == "read_only":
        # A completed catalog/inspection report is evidence, not a proposed effect.
        # Calling it a preview creates a pending approval with nothing to approve.
        mode = "tool_executed" if dispatch.ok else "tool_failed"
    elif dispatch.status == "executed":
        mode = "tool_executed"
    elif dispatch.status in {"reported", "approval_required"}:
        mode = "tool_preview"
    else:
        mode = "tool_failed"
    return ToolIntentExecution(
        handled=True,
        ok=bool(dispatch.ok),
        status=str(dispatch.status),
        response_text=str(dispatch.response_text or ""),
        # The handler's own words for what the approved call did (moved, or why nothing was moved),
        # so the answer to an Allow is never the generic "wasn't able" fallback.
        user_safe_response_text=str(dispatch.response_text or "") if approved else "",
        mode=mode,
        tool_name=intent,
        details={
            **dict(dispatch.details or {}),
            "observation": _tool_observation(
                intent=intent,
                tool_surface="local_operator",
                ok=bool(dispatch.ok),
                status=str(dispatch.status),
                details=dict(dispatch.details or {}),
                response_preview=str(dispatch.response_text or "")[:280],
            ),
        },
        learned_plan=dispatch.learned_plan,
    )


#: Operator tools whose handler answers a call with a stored preview and a second, typed approval
#: ("Reply with: approve move <id>"). The tool executor builds every model call with
#: `approval_requested=False`, so after the operator allowed this exact call on the approval card the
#: replayed call stored a preview again and ended as a `tool_preview` with no approval id: the page
#: shows no card for that and waits on "Resuming pending tool" (captest round 2, a22771c2).
_APPROVED_PREVIEW_INTENTS = frozenset({"operator.move_path"})


def _run_approved_preview(
    operator_intent: OperatorActionIntent,
    dispatch: Any,
    *,
    task_id: str,
    session_id: str,
    dispatch_operator_action_fn: Any,
) -> Any:
    """The call the operator approved exactly (Windows only, `claim_operator_approved_call`): the
    preview its handler just stored from this call's own arguments is applied once, by the handler's
    own approval path (the Windows write gate on the stored paths, the local-action gate, the scope
    checks, the broker's rename once it is wired).

    Only a fresh preview of this call is applied: one the handler stored now from this call's paths,
    which it answers with that source and destination. A call the handler answered from a preview the
    store already held (one the call names by `action_id`, or the chat's newest) is refused in plain
    words and nothing is moved, since the operator approved this call's paths and not that preview's.
    Any other answer (a refusal, a conflict) stands as it is."""
    from core.local_operator_actions import OperatorActionResult

    if str(getattr(dispatch, "status", "") or "") != "approval_required":
        return dispatch
    details = dict(getattr(dispatch, "details", None) or {})
    action_id = str(details.get("action_id") or "").strip()
    # Only the handler's fresh-preview answer names the paths it stored; an answer from a preview the
    # store already held (named by `action_id`, or the chat's newest) carries the id alone.
    fresh = (
        bool(action_id)
        and bool(str(details.get("source_path") or "").strip())
        and bool(str(details.get("destination_path") or "").strip())
    )
    if not fresh:
        return OperatorActionResult(
            ok=False,
            status="blocked",
            response_text=(
                "This chat holds another move preview, so the move you approved was not matched to it. "
                "Nothing was moved; ask for the move again."
            ),
            details={"action_id": action_id} if action_id else {},
        )
    return dispatch_operator_action_fn(
        replace(
            operator_intent,
            approval_requested=True,
            action_id=action_id,
            raw_text=f"approve move {action_id}",
        ),
        task_id=task_id,
        session_id=session_id,
    )


#: Operator tools whose contract input is ``{"text": "the request as written"}``.
_REQUEST_TEXT_KINDS = frozenset({"check_availability", "propose_calendar_event", "update_calendar_event", "save_note"})


def build_operator_action_intent(operator_kind: str, arguments: dict[str, Any]) -> OperatorActionIntent:
    target_path = str(arguments.get("target_path") or arguments.get("path") or "").strip() or None
    destination_path = str(arguments.get("destination_path") or arguments.get("destination_dir") or "").strip() or None
    raw_text = ""
    if operator_kind == "move_path":
        source = str(arguments.get("source_path") or target_path or "").strip()
        dest = str(destination_path or "").strip()
        raw_text = f'move "{source}" to "{dest}"'.strip()
        target_path = source or None
    elif operator_kind == "schedule_calendar_event":
        title = str(arguments.get("title") or "VOOL Meeting").strip()
        start_iso = str(arguments.get("start_iso") or "").strip()
        duration_minutes = max(15, int(arguments.get("duration_minutes") or 30))
        raw_text = f'schedule a meeting "{title}" on {start_iso} for {duration_minutes}m'.strip()
    elif operator_kind in _REQUEST_TEXT_KINDS:
        # These contracts take the request as written (``text``) and their handlers read it the
        # way they read a chat request. Approval is never read from it: approval_requested stays
        # False below, so a proposal or update can only stage, never apply.
        raw_text = str(arguments.get("text") or "").strip()
    elif operator_kind == "cleanup_temp_files" and target_path:
        raw_text = f'clean temp files in "{target_path}"'
    elif operator_kind == "inspect_disk_usage" and target_path:
        raw_text = f'find disk bloat in "{target_path}"'
    return OperatorActionIntent(
        kind=operator_kind,
        target_path=target_path,
        destination_path=destination_path,
        approval_requested=False,
        action_id=str(arguments.get("action_id") or "").strip() or None,
        raw_text=raw_text,
    )
