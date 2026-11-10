"""Windows: after a step ran, the local model's plain-text answer closes the turn.

Captest (Windows 11 VM, build b58c5eb0, qwen3:8b on the bundled Ollama, CPU only): "run calc.py in
C:\\Users\\VOOLT\\VOOL Test\\Marked A", Allow, the job ran within seconds, and about 8.7 minutes
later the turn ended with "The model's follow-up step failed, so this is the tool output without a
written answer." The recorded cause was "malformed_tool_arguments:required native tool call is
missing". The same happened for p.py (print(2+3)).

Why: every round of the model tool loop is a tool_intent round (`resolve_tool_intent` ->
`_build_round_request`, `tool_choice="required"`, `tools_required=True`). That is by design: the
loop ends when the model calls `respond.direct` ("the native no-more-tools branch",
core/cloud_tool_call_contract.py). Cloud lanes honour `tool_choice: required`. Ollama ignores it,
and after a tool result qwen3:8b writes its answer as prose. The Ollama lane then fails closed
(`MalformedToolArgumentsError("required native tool call is missing")`) and the answer the model
spent eight minutes writing is thrown away.

The fix, Windows only for 0.7.1 (the seam the approval spend uses,
`execution_gate.operator_approval_spend_applies`): a loop round that follows a step that RAN is
marked as a follow-up (`tool_followup_after_run` on the loop context, carried to the request's
metadata by `_build_round_request`). On a marked round the Ollama lane takes non-empty prose with
no tool call as the `respond.direct` it stands for, when `respond.direct` was offered. Everything
else is as it was: an unmarked round (the first round, Mac, Linux) still fails closed on prose, an
empty or reasoning-only reply still fails closed, and a real tool call is kept as it came.
"""
from __future__ import annotations

import http.server
import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from adapters.base_adapter import ModelRequest
from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from core import runtime_flags
from core.cloud_tool_call_contract import MalformedToolArgumentsError, build_cloud_tool_definitions
from tests.test_the_tool_loop_reports_what_it_actually_ran import _call, _drive

FOLLOWUP_KEY = "tool_followup_after_run"
PROSE = "calc.py printed `calc ok 5 20`: the script ran and exited with code 0."


@pytest.fixture
def windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)


@pytest.fixture
def off_windows_seam(monkeypatch):
    from sandbox import job_runner

    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)


# --- the loop marks the round that follows a step that ran ----------------------------------------


def test_the_key_is_the_one_the_runtime_defines() -> None:
    from core.execution_gate import TOOL_FOLLOWUP_AFTER_RUN_KEY

    assert TOOL_FOLLOWUP_AFTER_RUN_KEY == FOLLOWUP_KEY


def test_on_windows_the_round_after_a_run_is_marked_as_a_followup(windows_seam) -> None:
    result, router, _events, _ws = _drive([[_call("workspace.read_file", path="f00.py")]])

    assert len(router.contexts) == 2, router.calls
    assert not router.contexts[0].get(FOLLOWUP_KEY), "the first round follows nothing that ran"
    assert router.contexts[1].get(FOLLOWUP_KEY) is True, "the round after the read was not marked"
    assert list((result.get("details") or {}).get("tool_steps") or []) == ["workspace.read_file"]


def test_off_windows_no_round_is_marked(off_windows_seam) -> None:
    _result, router, _events, _ws = _drive([[_call("workspace.read_file", path="f00.py")]])

    assert len(router.contexts) == 2, router.calls
    assert not any(context.get(FOLLOWUP_KEY) for context in router.contexts), router.contexts


def test_on_windows_a_round_with_nothing_run_is_not_marked(windows_seam) -> None:
    """The model closes on the first round: no step ran, so no round is a follow-up."""
    _result, router, _events, _ws = _drive([])

    assert router.contexts and not any(context.get(FOLLOWUP_KEY) for context in router.contexts)


# --- the round request carries the mark to the adapter --------------------------------------------


def _round_request(source_context: dict):
    from core.human_input_adapter import HumanInputInterpretation
    from core.identity_manager import load_active_persona
    from core.memory_first_router import MemoryFirstRouter
    from core.task_router import classify, create_task_record
    from core.tool_offer_state import begin_turn_navigation, end_turn_navigation, reset_offer_state

    text = "run calc.py"
    task = create_task_record(text)
    interpretation = HumanInputInterpretation(
        raw_text=text, normalized_text=text, reconstructed_text=text, intent_mode="command",
        topic_hints=[], reference_targets=[], understanding_confidence=0.9, quality_flags=[],
    )
    classification = classify(text, context=interpretation.as_context())
    context_result = SimpleNamespace(
        assembled_context=lambda: "", context_snippets=lambda: [], local_candidates=[], swarm_metadata=[],
        retrieval_confidence_score=0.0,
        report=SimpleNamespace(retrieval_confidence=0.0, total_tokens_used=lambda: 0,
                               to_dict=lambda: {"external_evidence_attachments": []}),
    )
    session = "openclaw:followup-" + uuid.uuid4().hex
    context = {
        "surface": "openclaw", "platform": "openclaw", "runtime_session_id": session, "session_id": session,
        "turn_id": "turn-" + uuid.uuid4().hex, **source_context,
    }
    reset_offer_state()
    scope = begin_turn_navigation(context)
    try:
        return MemoryFirstRouter()._build_round_request(
            task=task, classification=classification, interpretation=interpretation,
            context_result=context_result, persona=load_active_persona("default"), output_mode="tool_intent",
            task_kind="tool_intent", surface="openclaw", source_context=context,
        )
    finally:
        end_turn_navigation(context, scope)
        reset_offer_state()


def test_a_marked_round_request_carries_the_mark_and_still_requires_a_tool() -> None:
    from core import policy_engine

    previous = policy_engine._POLICY_CACHE
    try:
        request = _round_request({FOLLOWUP_KEY: True})
    finally:
        policy_engine._POLICY_CACHE = previous

    assert request.metadata.get(FOLLOWUP_KEY) is True, request.metadata
    # The contract of the round is unchanged: cloud lanes are still asked for a call.
    assert request.tool_choice == "required" and request.tools_required


def test_an_unmarked_round_request_carries_no_mark() -> None:
    from core import policy_engine

    previous = policy_engine._POLICY_CACHE
    try:
        request = _round_request({})
    finally:
        policy_engine._POLICY_CACHE = previous

    assert FOLLOWUP_KEY not in request.metadata, request.metadata


# --- the Ollama lane: prose on a marked round is the respond.direct it stands for -----------------


class _Handler(http.server.BaseHTTPRequestHandler):
    body: bytes = b"{}"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def ollama():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def answer(message: dict):
        _Handler.body = json.dumps({"model": "qwen3:8b", "message": message, "done": True}).encode("utf-8")
        return OpenAICompatibleAdapter(
            SimpleNamespace(
                provider_name="ollama-local", provider_id="ollama-local:qwen3:8b", model_name="qwen3:8b",
                metadata={"runtime_family": "ollama"},
                runtime_config={"base_url": f"http://127.0.0.1:{port}", "timeout_seconds": 5.0},
            )
        )

    try:
        yield answer
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _Handler.body = b"{}"


RUN = {"intent": "sandbox.run_command", "description": "Run a bounded command.", "arguments": {"command": "string"}}
RESPOND = {"intent": "respond.direct", "description": "Answer the user.", "arguments": {"message": "string"}}


def _request(*, marked: bool, offer=(RUN, RESPOND)) -> ModelRequest:
    return ModelRequest(
        task_kind="tool_intent",
        prompt="run calc.py",
        output_mode="tool_intent",
        messages=[{"role": "user", "content": "run calc.py"}],
        tools=build_cloud_tool_definitions(list(offer)),
        tool_choice="required",
        tools_required=True,
        metadata={FOLLOWUP_KEY: True} if marked else {},
    )


def _run(adapter, request):
    with runtime_flags.override("ollama_native_tools", True):
        return adapter.run_structured_task(request)


def test_on_a_marked_round_prose_closes_the_turn_as_respond_direct(ollama, windows_seam) -> None:
    response = _run(ollama({"role": "assistant", "content": PROSE}), _request(marked=True))

    assert [call.intent for call in response.tool_calls] == ["respond.direct"], response.tool_calls
    assert response.tool_calls[0].arguments == {"message": PROSE}
    assert json.loads(response.output_text) == {"intent": "respond.direct", "arguments": {"message": PROSE}}


def test_a_leading_reasoning_block_is_not_part_of_the_answer(ollama, windows_seam) -> None:
    content = f"<think>The user ran calc.py; summarise.</think>\n{PROSE}"
    response = _run(ollama({"role": "assistant", "content": content}), _request(marked=True))

    assert response.tool_calls[0].arguments == {"message": PROSE}, response.tool_calls


def test_on_an_unmarked_round_prose_still_fails_closed(ollama) -> None:
    """The first round of a turn, and every round on Mac and Linux, are unchanged."""
    with pytest.raises(MalformedToolArgumentsError, match="required native tool call is missing"):
        _run(ollama({"role": "assistant", "content": PROSE}), _request(marked=False))


def test_a_marked_round_without_respond_direct_on_offer_still_fails_closed(ollama, windows_seam) -> None:
    with pytest.raises(MalformedToolArgumentsError, match="required native tool call is missing"):
        _run(ollama({"role": "assistant", "content": PROSE}), _request(marked=True, offer=(RUN,)))


@pytest.mark.parametrize(
    "content", ["", "   ", "<think>Only reasoning, no answer.</think>"], ids=["empty", "blank", "reasoning-only"]
)
def test_a_marked_round_with_no_answer_still_fails_closed(ollama, windows_seam, content: str) -> None:
    with pytest.raises(MalformedToolArgumentsError, match="required native tool call is missing"):
        _run(ollama({"role": "assistant", "content": content}), _request(marked=True))


def test_a_real_tool_call_on_a_marked_round_is_kept_as_it_came(ollama, windows_seam) -> None:
    native = build_cloud_tool_definitions([RUN, RESPOND])[0].name
    message = {
        "role": "assistant", "content": "Running it again.",
        "tool_calls": [{"function": {"name": native, "arguments": {"command": "python calc.py"}}}],
    }
    response = _run(ollama(message), _request(marked=True))

    assert [call.intent for call in response.tool_calls] == ["sandbox.run_command"], response.tool_calls
    assert response.tool_calls[0].arguments == {"command": "python calc.py"}
