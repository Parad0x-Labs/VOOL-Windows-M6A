"""A generated file waiting for permission is not an empty model completion."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from core.agent_runtime.builder import app_builder as ab
from core.agent_runtime.builder import controller, mutation_scope, pinned_generation
from core.mode_permission_policy import decide_tool_call, reset_mode_permission_state, set_active_mode
from core.tool_intent_executor import ToolIntentExecution

CANARY = "m6b-denied-canary-20261010.txt"
CONTENT = "M6B_SYNTHETIC_CANARY"
REQUEST = (
    f"Create a plain text file named {CANARY} in the active project folder with exactly this content: {CONTENT}. "
    "Use the workspace file tool, not a terminal command. Do not modify any other file."
)
MODEL = "vool-qwen3-4b-instruct:nothink-m6b"


@pytest.fixture
def agent():
    from apps.vool_agent import VoolAgent

    reset_mode_permission_state()
    yield VoolAgent(backend_name="test-backend", device="test", persona_id="default")
    reset_mode_permission_state()


@pytest.mark.parametrize("phase", ["directory", "write", "test", "fix"])
@pytest.mark.parametrize("marker", ["mode", "status"])
def test_build_pauses_at_first_preview_without_later_tools_or_fix_generations(phase, marker):
    calls, generations = [], []
    pending = ab.ToolOutcome(False, "Waiting for approval.", {"approval_request": {"id": "exact-preview"}})
    setattr(pending, marker, "tool_preview" if marker == "mode" else "pending_approval")

    def generate(prompt):
        generations.append(prompt)
        if "JSON array of relative file path" in prompt:
            return json.dumps(["main.py", "test_main.py"])
        return "changed = True" if "tests FAILED" in prompt else "initial = True"

    def execute(intent, arguments, **kwargs):
        calls.append((intent, arguments))
        matches = (
            (phase == "directory" and intent == "workspace.ensure_directory")
            or (phase == "write" and intent == "workspace.write_file")
            or (phase == "test" and intent == "sandbox.run_command")
            or (phase == "fix" and arguments.get("content") == "changed = True")
        )
        if matches:
            return pending
        if intent == "sandbox.run_command":
            return ab.ToolOutcome(False, "FAILED (failures=1)", {"returncode": 1})
        return ab.ToolOutcome(True)

    report = ab.build_app_from_spec(
        request="build a Python app with tests", target_rel="app", source_context={},
        generate_fn=generate, run_tool_fn=execute,
    )
    assert getattr(report, "pending_approval", None) is pending
    assert len(calls) == {"directory": 1, "write": 2, "test": 4, "fix": 5}[phase]
    assert len(generations) == (4 if phase == "fix" else 3)
    assert not report.files_skipped
    assert report.tests_ran is (phase == "fix")
    assert not report.tests_passed


def _drive(agent, tmp_path, *, pinned=True, content=CONTENT, source_context=None, request=REQUEST):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    set_active_mode("pending-chat", "manual", client_turn_id="pending-turn")
    context = source_context if source_context is not None else {
        "runtime_session_id": "pending-chat", "cancel_turn_id": "pending-turn",
        "operating_mode": "manual", "surface": "api",
        "workspace": str(workspace), "workspace_root": str(workspace),
    }
    if pinned:
        context["requested_model"] = MODEL
    previews, calls = [], []
    generate = mock.Mock(return_value=content)

    def execute(payload, *, task_id, source_context, **kwargs):
        calls.append(payload)
        decision = decide_tool_call(
            intent=payload["intent"], arguments=payload["arguments"], task_id=task_id, source_context=source_context,
        )
        approval = dict(decision.approval_request or {})
        assert approval, "Manual mode must still gate this write"
        previews.append(approval)
        return ToolIntentExecution(
            handled=True, ok=False, status="pending_approval", mode="tool_preview",
            tool_name=payload["intent"], response_text="Waiting for approval for the proposed file write.",
            details={"approval_request": approval},
        )

    with (
        mock.patch.object(agent.memory_router, "_requested_model_manifest", return_value=SimpleNamespace(
            provider_id="ollama-local", model_name=MODEL,
        )),
        mock.patch.object(pinned_generation, "build_pinned_generate_fn", return_value=generate),
        mock.patch.object(controller, "build_ollama_generate_fn", return_value=generate),
        mock.patch.object(agent, "_execute_tool_intent", side_effect=execute),
        mock.patch.object(agent, "_emit_runtime_event", return_value={}),
    ):
        result = controller._run_model_build(
            agent, task=SimpleNamespace(task_id="pending-build"), effective_input=request,
            classification={"task_class": "system_design"}, interpretation=None,
            session_id="pending-chat", source_context=context,
            target={"root_dir": "generated/app"}, load_active_persona_fn=lambda: None,
        )
    return result, previews, calls, generate


@pytest.mark.parametrize("pinned", [True, False])
def test_exact_canary_returns_real_manual_approval_not_empty_completion(agent, tmp_path, pinned):
    result, previews, calls, generate = _drive(agent, tmp_path, pinned=pinned)
    assert result["mode"] == "tool_preview"
    assert result.get("task_outcome") == "pending_approval"
    assert result["response_class"] == "approval_required"
    assert result["success"] is False
    assert result["details"]["approval_request"] == previews[0]
    assert result["source_context"]["persist_memory"] is False
    assert calls == [{"intent": "workspace.write_file", "arguments": {"path": CANARY, "content": CONTENT}}]
    assert generate.call_count == 1
    assert not (tmp_path / "ws" / CANARY).exists()
    assert "empty" not in result["response"].lower()


def test_genuinely_empty_generation_still_fails_without_requesting_permission(agent, tmp_path):
    result, previews, calls, generate = _drive(agent, tmp_path, content="")
    assert result["route_reason"] == "builder_pinned_model_produced_nothing"
    assert result["success"] is False
    assert result["mode"] == "advice_only"
    assert not previews and not calls
    assert generate.call_count == 1


def test_full_canary_parent_preserves_approval_and_never_dispatches_later_clause(agent, tmp_path):
    from core.turn_contract import TURN_REQUEST_KEY, TurnRequest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = {
        "runtime_session_id": "pending-chat", "cancel_turn_id": "pending-turn",
        "operating_mode": "manual", "surface": "api", "requested_model": MODEL,
        "workspace": str(workspace), "workspace_root": str(workspace),
    }
    context[TURN_REQUEST_KEY] = TurnRequest.from_ingress(
        user_text=REQUEST, source_context=context, request_id="pending-request",
        turn_id="pending-turn", session_id="pending-chat",
    )
    dispatched, previews = [], []

    def child(text, *, source_context, **kwargs):
        dispatched.append(text)
        assert source_context[TURN_REQUEST_KEY] is context[TURN_REQUEST_KEY]
        result, approval, calls, generate = _drive(
            agent, tmp_path, source_context=source_context, request=text,
        )
        previews.extend(approval)
        return result

    with (
        mock.patch.object(agent, "_run_once_inner", side_effect=child),
        mock.patch.object(agent, "_emit_runtime_event", return_value={}) as events,
    ):
        result = agent._maybe_answer_demand_owned_turn(
            raw_input=REQUEST, effective_input=REQUEST, session_id="pending-chat", source_context=context,
        )
    assert result is not None
    assert len(dispatched) == 1
    assert result["mode"] == "tool_preview"
    assert result.get("task_outcome") == "pending_approval"
    assert result["details"]["approval_request"] == previews[0]
    parent_previews = [call.kwargs for call in events.call_args_list if call.kwargs.get("event_type") == "tool_preview"]
    assert len(parent_previews) == 1
    assert parent_previews[0]["approval_request"] == previews[0]
    assert "empty" not in result["response"].lower()
    assert "failed" not in result["route_reason"]


def test_sequential_plan_pause_retains_exact_preview_and_unattempted_siblings():
    from core.agent_runtime.turn_planner import PlannedTask, PlannedTaskNeedsApprovalError, run_plan

    tasks = [PlannedTask(index=0, request="write a file"), PlannedTask(index=1, request="run tests")]
    preview = {"mode": "tool_preview", "details": {"approval_request": {"id": "exact-preview"}}}
    calls = []

    def run_one(task, done):
        calls.append(task.index)
        raise PlannedTaskNeedsApprovalError("Waiting for approval.", pending_result=preview)

    outcomes = run_plan(tasks, run_one=run_one, max_workers=1, stop_on_approval=True)
    assert calls == [0]
    assert len(outcomes) == len(tasks)
    assert outcomes[0].pending_result == preview
    assert outcomes[0].needs_approval and not outcomes[0].ok
    assert outcomes[1].outcome_kind == "not_attempted"
    assert not outcomes[1].ok and not outcomes[1].needs_approval


def test_pending_plan_is_not_a_failed_or_executed_demand():
    from core.agent_runtime.demand_ownership import demand_records, units_as_plan
    from core.agent_runtime.turn_planner import TaskOutcome

    tasks = units_as_plan(REQUEST)
    assert len(tasks) == 2
    records = demand_records(REQUEST, [
        TaskOutcome(task=tasks[0], outcome_kind="pending_approval", pending_message="Waiting for approval."),
        TaskOutcome(task=tasks[1], outcome_kind="not_attempted"),
    ])
    assert [(row.attempted, row.terminal_state) for row in records] == [
        (True, "pending_approval"), (False, "not_attempted"),
    ]


def test_nonpausing_plan_keeps_its_existing_independent_sibling_behavior():
    from core.agent_runtime.turn_planner import PlannedTask, PlannedTaskNeedsApprovalError, run_plan

    tasks = [PlannedTask(index=0, request="write"), PlannedTask(index=1, request="explain")]
    calls = []

    def run_one(task, done):
        calls.append(task.index)
        if task.index == 0:
            raise PlannedTaskNeedsApprovalError("Waiting for approval.")
        return "Independent answer."

    outcomes = run_plan(tasks, run_one=run_one, max_workers=1)
    assert calls == [0, 1]
    assert outcomes[0].needs_approval and outcomes[1].ok


def test_parallel_approval_pause_is_refused_before_dispatch():
    from core.agent_runtime.turn_planner import PlannedTask, run_plan

    run_one = mock.Mock(side_effect=AssertionError("no dispatch is authorized"))
    with pytest.raises(ValueError, match="sequentially"):
        run_plan([PlannedTask(index=0, request="write")], run_one=run_one, max_workers=2, stop_on_approval=True)
    run_one.assert_not_called()


def test_unattempted_outcome_cannot_claim_success_from_stale_answer_text():
    from core.agent_runtime.turn_planner import PlannedTask, TaskOutcome

    outcome = TaskOutcome(
        task=PlannedTask(index=0, request="write"),
        answer="Old answer.", outcome_kind="not_attempted",
    )
    assert not outcome.ok and not outcome.needs_approval
