"""A model that answers in prose where a tool call was required did answer: it is not a failed tool.

Captest (Windows 11, qwen3:8b on the bundled Ollama): "What folder are you looking at?". The word
"folder" opens the tool loop, every round of which requires a tool call. Ollama ignores
`tool_choice: required`, qwen answered in prose, and the adapter refused that reply as
"required native tool call is missing". The loop then recorded a `tool_failed` event whose message
read "No usable provider reply was available for this step, so no tool was selected", and the
chat page drew it as "TOOL CALLS (1): Tool failed" under a correct prose answer.

Both are untrue: the provider replied, and no tool was chosen or run. These rows pin the honest
record. When the router says the model WAS attempted and the cause is a missing or malformed tool
call, the loop emits a non-tool runtime event instead of `tool_failed`, and everything else the
loop decides from that round stays as it was. A real timeout, or a refusal whose dispatch is not
proven, keeps the old `tool_failed` record.

Only the model's replies are scripted (at the router seam); the loop, tools and workspace are
real. Every row is host-independent: the Windows seam is set explicitly where it matters.
"""
from __future__ import annotations

import json
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest

from apps.vool_agent import VoolAgent
from core.memory_first_router import ModelExecutionDecision

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOGS = ROOT / "core" / "i18n" / "catalogs"

EVENT = "model_no_tool_selected"
MISSING = "malformed_tool_arguments:required native tool call is missing"
MALFORMED = "malformed_tool_arguments:arguments for workspace.read_file are not valid JSON"
ANSWERED = "The model answered without choosing a tool; nothing was run."
UNUSABLE = "The model's tool call could not be used, so no tool was selected and nothing was run."
FALSE_NOTE = "No usable provider reply"

FILES = {"f00.py": "# f0\nUNIQ_00 = 0\n", "f01.py": "# f1\nUNIQ_01 = 1\n"}


def _call(intent: str, **arguments):
    return SimpleNamespace(intent=intent, arguments=arguments, call_id=f"c-{intent}", name=intent)


def _refused(reason: str, *, attempted: bool | None = True) -> ModelExecutionDecision:
    """What the router returns when the selection round's reply was refused or never came."""
    details: dict = {"reason": reason, "block_reason": reason}
    if attempted is not None:
        details["model_was_attempted"] = attempted
    return ModelExecutionDecision(
        source="selected_model_blocked", task_hash="h", used_model=False,
        structured_output=None, provider_id=None, validation_state="not_run", details=details,
    )


class _Router:
    def __init__(self, scripts, decision_for_round):
        self.scripts = list(scripts)
        self.decision_for_round = decision_for_round
        self.calls = 0
        self.synthesis_calls = 0

    def resolve(self, **_kwargs):
        self.synthesis_calls += 1
        return SimpleNamespace(
            output_text="Grounded synthesis over the tool results.",
            provider_id="p", used_model=True, confidence=0.8, trust_score=0.8,
            validation_state="validated", details={}, source="provider_execution",
            model_name="m", provider_name="p", cache_hit=False, candidate_id=None,
            failover_used=False, structured_output=None, task_hash="h",
        )

    def resolve_tool_intent(self, **_kwargs):
        index = self.calls
        self.calls += 1
        override = self.decision_for_round(index)
        if override is not None:
            return override
        if index < len(self.scripts):
            head = self.scripts[index][0]
            return ModelExecutionDecision(
                source="scripted", task_hash="h", used_model=True, confidence=0.8,
                structured_output={"intent": head.intent, "arguments": dict(head.arguments)},
                tool_calls=tuple(self.scripts[index]), validation_state="validated", provider_id="p",
            )
        closing = "All done."
        return ModelExecutionDecision(
            source="scripted", task_hash="h", used_model=True, confidence=0.8,
            structured_output={"intent": "respond.direct", "arguments": {"message": closing}},
            tool_calls=(_call("respond.direct", message=closing),),
            validation_state="validated", provider_id="p",
        )


def _drive(tmp_path, scripts, decision_for_round):
    workspace = pathlib.Path(tmp_path) / "ws"
    workspace.mkdir()
    for name, body in FILES.items():
        (workspace / name).write_text(body, encoding="utf-8")
    router = _Router(scripts, decision_for_round)
    events: list[dict] = []
    agent = VoolAgent.__new__(VoolAgent)
    agent.memory_router = router
    agent._should_keep_ai_first_chat_lane = lambda **_kw: False
    agent._should_run_builder_controller = lambda **_kw: False
    agent._plan_tool_workflow = lambda **_kw: SimpleNamespace(
        handled=False, stop_after=False, next_payload=None, reason=""
    )
    agent.hive_activity_tracker = None
    agent.public_hive_bridge = None
    real_emit = agent._emit_runtime_event

    def _spy(context, **payload):
        events.append(payload)
        return real_emit(context, **payload)

    agent._emit_runtime_event = _spy
    context = {
        "surface": "api", "workspace": str(workspace), "workspace_root": str(workspace),
        "workspace_binding": "project", "project_id": workspace.name,
    }
    result = agent._maybe_execute_model_tool_intent(
        task=SimpleNamespace(task_id="t1"),
        effective_input="What folder are you looking at?",
        classification={"task_class": "unknown"},
        interpretation=SimpleNamespace(),
        context_result=SimpleNamespace(),
        persona=SimpleNamespace(),
        session_id=f"openclaw:{uuid.uuid4().hex[:20]}",
        source_context=context,
        surface="api",
    )
    return result or {}, router, events


def _of(events, event_type):
    return [e for e in events if e.get("event_type") == event_type]


def _blob(events) -> str:
    return json.dumps(events, default=str)


@pytest.fixture
def windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)


@pytest.fixture
def off_windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)


@pytest.mark.parametrize("seam", ["windows_seam", "off_windows_seam"])
def test_prose_where_a_call_was_required_is_recorded_as_answered_without_a_tool(tmp_path, request, seam):
    request.getfixturevalue(seam)
    result, router, events = _drive(tmp_path, [], lambda _i: _refused(MISSING))

    assert _of(events, "tool_failed") == [], _of(events, "tool_failed")
    assert FALSE_NOTE not in _blob(events)
    honest = _of(events, EVENT)
    assert len(honest) == 1, events
    assert honest[0].get("message") == ANSWERED
    assert honest[0].get("status") == "answered_without_tool"
    assert honest[0].get("tool_name") in (None, "")
    # Nothing ran, so the turn is still handed on to the plain answer, exactly as before.
    assert result == {}, result
    assert router.synthesis_calls == 0


@pytest.mark.parametrize("seam", ["windows_seam", "off_windows_seam"])
def test_a_malformed_call_is_named_as_unusable_not_as_a_missing_provider_reply(tmp_path, request, seam):
    request.getfixturevalue(seam)
    result, _router, events = _drive(tmp_path, [], lambda _i: _refused(MALFORMED))

    assert _of(events, "tool_failed") == []
    assert FALSE_NOTE not in _blob(events)
    honest = _of(events, EVENT)
    assert [e.get("message") for e in honest] == [UNUSABLE]
    assert honest[0].get("status") == "tool_call_unusable"
    assert result == {}


@pytest.mark.parametrize("attempted", [None, False])
def test_without_proof_of_dispatch_the_old_record_is_kept(tmp_path, attempted):
    """`model_was_attempted` absent: nothing proves the model saw the request, so the provider
    record stands. False: the existing refused-before-send record stands."""
    _result, _router, events = _drive(tmp_path, [], lambda _i: _refused(MISSING, attempted=attempted))

    assert _of(events, EVENT) == []
    failures = _of(events, "tool_failed")
    assert len(failures) == 1, events
    if attempted is None:
        assert FALSE_NOTE in str(failures[0].get("message"))
    else:
        assert failures[0].get("status") == "tool_selection_refused_before_send"


@pytest.mark.parametrize(
    "reason",
    ["provider timeout: Read timed out (read timeout=720.0)", "EMPTY_PROVIDER_RESPONSE"],
    ids=["timeout", "empty"],
)
def test_a_provider_that_did_not_answer_still_says_so(tmp_path, reason):
    _result, _router, events = _drive(tmp_path, [], lambda _i: _refused(reason))

    assert _of(events, EVENT) == []
    failures = _of(events, "tool_failed")
    assert len(failures) == 1 and FALSE_NOTE in str(failures[0].get("message")), events


def test_after_a_step_ran_the_windows_followup_ending_is_unchanged(tmp_path, windows_seam):
    scripts = [[_call("workspace.read_file", path="f00.py")]]
    result, router, events = _drive(tmp_path, scripts, lambda i: _refused(MISSING) if i >= 1 else None)

    details = result.get("details") or {}
    assert details.get("loop_stop_reason") == "provider_did_not_answer", details
    assert str(details.get("followup_failure") or "").startswith("malformed_tool_arguments"), details
    assert "UNIQ_00" in str(result.get("response") or "")
    assert router.synthesis_calls == 0
    assert _of(events, "tool_failed") == []
    assert [e.get("message") for e in _of(events, EVENT)] == [ANSWERED]


def test_after_steps_ran_off_windows_synthesis_is_unchanged(tmp_path, off_windows_seam):
    # Two reads: a single workspace read is rendered directly, with no synthesis to observe.
    scripts = [[_call("workspace.read_file", path="f00.py")], [_call("workspace.read_file", path="f01.py")]]
    result, router, events = _drive(tmp_path, scripts, lambda i: _refused(MISSING) if i >= 2 else None)

    assert (result.get("details") or {}).get("loop_stop_reason") == "provider_did_not_answer"
    assert router.synthesis_calls == 1
    assert _of(events, "tool_failed") == []
    assert [e.get("message") for e in _of(events, EVENT)] == [ANSWERED]


def test_the_event_is_not_an_execution_and_not_a_tool_receipt():
    from core import execution_truth
    from core.agent_runtime import orchestrator

    assert EVENT not in execution_truth._EXECUTION_EVENT_RULES
    assert EVENT not in orchestrator._TERMINAL_TOOL_EVENT_TYPES
    assert not EVENT.startswith("tool_"), "a tool_* type is drawn as a tool row by the chat page"


def _chat_page() -> str:
    from core.vool_chat_page import render_vool_chat_html

    return render_vool_chat_html()


def test_the_chat_page_files_it_as_a_runtime_row_not_a_tool_call():
    page = _chat_page()
    ledger = re.search(r"const LEDGER_MAP = \{(.*?)\n\};", page, re.DOTALL)
    assert ledger
    row = re.search(EVENT + r":\s*\[\s*'(\w+)'\s*,\s*'[^']*'\s*,\s*'([^']+)'\s*\]", ledger.group(1))
    assert row, "the event has no LEDGER_MAP label"
    assert row.group(1) != "fail", "answering without a tool is not a failure row"
    for table in ("ACTIVITY_TOOL_START_TYPES", "ACTIVITY_TOOL_END_TYPES"):
        found = re.search(r"const " + table + r" = \{([^}]*)\}", page)
        assert found and EVENT not in found.group(1), table


def test_every_catalog_labels_the_event():
    key = f"activity.ledger.{EVENT}"
    files = sorted(CATALOGS.glob("*.json"))
    assert files
    for path in files:
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        assert str(messages.get(key) or "").strip(), f"{path.name} lacks {key}"
