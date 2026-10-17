"""Per-invocation measurement, internal routing identity and independent missing-call detection."""
from __future__ import annotations

import json
import uuid
from unittest import mock

import pytest

from core import execution_truth, intent_arbiter
from core.provider_verification import build_verification_receipt
from core.runtime_task_events import emit_runtime_event
from core.turn_model_call_ledger import (
    begin_turn,
    record_provider_call,
    record_provider_call_outcome,
    turn_call_accounting,
    turn_model_calls,
)

PIN = "vool-qwen3-4b-instruct:nothink-m6b"


def context():
    turn = uuid.uuid4().hex
    ctx = {"request_id": turn, "cancel_turn_id": turn, "session_id": "measurement-" + turn,
           "requested_model": PIN}
    begin_turn(ctx)
    return ctx


def close_call(ctx, *, outcome="completed", model=PIN, role=""):
    call = record_provider_call(ctx, provider_id="ollama", model_id=model, cost_class="free_local", call_role=role)
    assert call
    assert record_provider_call_outcome(ctx, call, outcome=outcome)
    return call


def summary(ctx):
    return execution_truth.turn_execution_summary(ctx["cancel_turn_id"], session_id=ctx["session_id"])


def lane(ctx):
    emit_runtime_event(ctx, event_type="model_lane_proof", message="Completed lane",
                       details={"phase": "completed", "provider_id": "ollama", "model_id": PIN})


def test_failed_and_internal_calls_agree_with_the_turn_ledger():
    ctx = context()
    close_call(ctx, outcome="failed")
    close_call(ctx, outcome="failed", model="qwen3:4b", role="intent_arbiter")
    close_call(ctx)
    result = summary(ctx)
    assert result["model_calls"] == turn_model_calls(ctx) == 3
    assert len(result["failed_operations"]) == 2
    assert len(result["executed_operations"]) == 1
    assert result["ran_model"] and not result["ran_tool"] and not result["ran_effect"]
    assert result["witness"]["consistent"]


@pytest.mark.parametrize("order", ["lane-first", "receipt-first"])
def test_lane_summary_does_not_add_a_second_call(order):
    ctx = context()
    if order == "lane-first":
        lane(ctx)
    close_call(ctx)
    if order == "receipt-first":
        lane(ctx)
    assert summary(ctx)["model_calls"] == 1
    assert len(execution_truth.facts_for_turn(ctx["cancel_turn_id"], session_id=ctx["session_id"])) == 1
    assert summary(ctx)["witness"]["consistent"]


def test_legacy_proof_remains_readable_without_invocation_receipts():
    ctx = context()
    lane(ctx)
    assert summary(ctx)["model_calls"] == 1


def test_duplicate_receipt_does_not_duplicate_the_invocation():
    ctx = context()
    close_call(ctx, outcome="failed")
    receipt = turn_call_accounting(ctx)["verification_receipts"][0]
    emit_runtime_event(ctx, event_type="model_verification_receipt", message="Duplicate",
                       details={"provider_id": "ollama", "model_id": PIN, "verification_receipt": receipt})
    assert summary(ctx)["model_calls"] == 1


def test_distinct_same_model_attempts_remain_distinct():
    ctx = context()
    close_call(ctx)
    close_call(ctx)
    assert summary(ctx)["model_calls"] == 2


def test_independent_witness_detects_a_missing_second_call(monkeypatch):
    ctx = context()
    close_call(ctx)
    with monkeypatch.context() as patch:
        patch.setattr(execution_truth, "record_execution", lambda **kw: "")
        close_call(ctx)
    verdict = execution_truth.verify_execution_truth(ctx["cancel_turn_id"], session_id=ctx["session_id"])
    assert not verdict.consistent, "A recorded first call must not hide a missing second invocation"


@pytest.mark.parametrize("event", ["model.call_started", "model.call_failed", "model_lane_selected"])
def test_pre_invocation_events_never_count_as_calls(event):
    ctx = context()
    emit_runtime_event(ctx, event_type=event, message="No entry", details={"provider_id": "ollama",
                       "model_id": PIN, "model_call_id": "not-entered", "reason": "circuit_open"})
    assert summary(ctx)["model_calls"] == 0


@pytest.mark.parametrize("defect", ["no-id", "started", "wrong-provider", "wrong-model"])
def test_incoherent_invocation_receipts_are_not_counted(defect):
    ctx = context()
    receipt = build_verification_receipt(call_id="call", request_id=ctx["request_id"], requested_model=PIN,
                                         selected_model=PIN, provider_id="ollama", outcome="failed")
    if defect == "no-id":
        receipt["call_id"] = None
    if defect == "started":
        receipt["outcome"] = "attempted"
    emit_runtime_event(ctx, event_type="model_verification_receipt", message="Invalid shape",
                       details={"provider_id": "wrong" if defect == "wrong-provider" else "ollama",
                                "model_id": "wrong" if defect == "wrong-model" else PIN,
                                "verification_receipt": receipt})
    assert summary(ctx)["model_calls"] == 0


def test_call_facts_do_not_cross_turn_or_session():
    ctx = context()
    close_call(ctx)
    other = context()
    assert summary(other)["model_calls"] == 0
    assert execution_truth.model_calls(ctx["cancel_turn_id"], session_id=other["session_id"]) == 0


def test_arbiter_receipt_names_the_internal_role_without_relabeling_the_pin(monkeypatch):
    monkeypatch.setenv("VOOL_INTENT_ARBITER", "1")
    monkeypatch.setenv("VOOL_ARBITER_MODEL", "qwen3:4b")
    intent_arbiter.reset_breaker()
    ctx = context()
    with mock.patch("requests.post", side_effect=RuntimeError("synthetic provider failure")) as post:
        assert intent_arbiter.arbitrate("Find the fixture folder", [], source_context=ctx) is None
    assert post.call_count == 1 and post.call_args.kwargs["json"]["model"] == "qwen3:4b"
    receipt = turn_call_accounting(ctx)["verification_receipts"][0]
    assert receipt["requested_model"] == PIN and receipt["selected_model"] == "qwen3:4b"
    assert receipt["call_role"] == "intent_arbiter"
    assert receipt["outcome"] == "failed" and not receipt["verification"]["independent_model_proof"]
    assert summary(ctx)["model_calls"] == 1
    assert ctx["requested_model"] == PIN


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_adapter_receipt_preserves_its_runtime_call_role(outcome):
    from adapters.base_adapter import ModelRequest, ModelResponse
    from core.memory_first_router import MemoryFirstRouter
    from core.model_health import reset_provider_health
    from core.model_registry import ModelRegistry
    from core.task_router import create_task_record
    from tests.test_v050_fastpath_authority_and_call_accounting import _delete_probe_manifests, _register_probe_manifest

    reset_provider_health()
    name = "measurement-probe-" + uuid.uuid4().hex[:8]
    registry = ModelRegistry()
    manifest = _register_probe_manifest(registry, name)
    ctx = context()
    ctx["model_call_role"] = "conductor_generation"
    adapter = mock.Mock()
    adapter.supports_streaming.return_value = False
    if outcome == "failed":
        adapter.run_text_task.side_effect = RuntimeError("synthetic failure")
    else:
        adapter.run_text_task.return_value = ModelResponse(output_text="fixture", raw_response={})
    try:
        with mock.patch.object(registry, "build_adapter", return_value=adapter), mock.patch(
            "core.memory_first_router.should_probe_health", return_value=False
        ):
            MemoryFirstRouter(registry)._invoke_manifest(manifest=manifest,
                request=ModelRequest(task_kind="summarize", prompt="fixture"), output_mode="plain_text",
                task=create_task_record("fixture"), source_context=ctx)
        receipt = turn_call_accounting(ctx)["verification_receipts"][0]
        assert receipt["call_role"] == "conductor_generation"
        assert receipt["selected_model"] == name and receipt["requested_model"] == PIN
        assert receipt["outcome"] == outcome
        assert summary(ctx)["model_calls"] == 1
    finally:
        _delete_probe_manifests((name,))


def test_role_is_bound_metadata_not_provider_attestation():
    from hashlib import sha256

    ctx = context()
    close_call(ctx, role="intent_arbiter")
    receipt = turn_call_accounting(ctx)["verification_receipts"][0]
    assert receipt["call_role"] == "intent_arbiter"
    facts = {key: value for key, value in receipt.items() if key not in {"binding_sha256", "verification"}}
    digest = sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    assert digest == receipt["binding_sha256"]
    assert receipt["verification"]["status"] == "CLAIMED"


def test_refused_dispatch_permit_never_counts_a_transport_entry(monkeypatch):
    monkeypatch.setenv("VOOL_INTENT_ARBITER", "1")
    monkeypatch.setenv("VOOL_ARBITER_MODEL", "qwen3:4b")
    intent_arbiter.reset_breaker()
    ctx = context()
    permit = mock.Mock()
    permit.consume.side_effect = RuntimeError("synthetic authorization revoked before transport")
    with mock.patch.object(intent_arbiter, "seal_direct_provider_invocation", return_value=permit), mock.patch(
        "requests.post", side_effect=AssertionError("Refused permit must not enter transport")
    ) as post:
        assert intent_arbiter.arbitrate("Find the fixture folder", [], source_context=ctx) is None
    post.assert_not_called()
    assert turn_model_calls(ctx) == 0
    assert turn_call_accounting(ctx)["verification_receipts"] == []
    assert summary(ctx)["model_calls"] == 0


@pytest.mark.parametrize("phase", ["completed", "pending_approval", "denied"])
def test_runtime_action_proof_is_not_model_inference(phase):
    ctx = context()
    emit_runtime_event(ctx, event_type="model_lane_proof", message="Runtime action without inference",
                       details={"phase": phase, "provider_id": "runtime-action", "model_id": "",
                                "actual_adapter_provider_id": "", "actual_adapter_model_id": "",
                                "backend": "tool_workflow", "fallback_reason": "model_not_used"})
    assert summary(ctx)["model_calls"] == 0
    assert not summary(ctx)["ran_model"]
