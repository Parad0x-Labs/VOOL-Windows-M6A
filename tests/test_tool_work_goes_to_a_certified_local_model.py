"""Tool work goes to a certified local model, not to a faster one that failed certification.

Capability follow-up (0.7.1-beta, clean Windows 11 VM, 16 GB RAM, no GPU): after qwen3:4b was
downloaded in Settings it certified "degraded" (missing_tool_calls, finish_reason length), and
qwen3:8b was installed and certified later. New chats still sent real work ("run the tests",
"fix a bug") to qwen3:4b, and those turns hung for 10+ minutes; only a per-chat header pin to
qwen3:8b got them off it.

The cause is the local inference autopilot's pick being promoted to the front of the candidate
list (``core.memory_first_router._prioritize_autopilot_selection``). The tool-intent call
(``MemoryFirstRouter.resolve_tool_intent``: role drone, output_mode tool_intent) is ranked with
certified local models first (``core.provider_routing``), and then the promotion, which is
unconditional when paid fallback is off, as it always is for a drone call, undoes that order. A tool_intent turn resolves to
the tiny lane, where a model of 4B or less scores +3.0 against +0.6 for an 8B one
(``core.local_inference_autopilot._primary_score``). Nothing in that pick, or in its promotion, reads
certification. The final-answer authorship fence refuses an uncertified author before it is called,
but a tool_intent call is not authorship (``precall_author_verdict`` returns None for it), so the
degraded model was the one that ran the tools.

The fix is at the promotion. An uncertified local pick is not moved ahead of a certified local
model, and the certified one is moved first instead. A model the user pinned in the chat header is
never replaced. When no certified local model exists the turn still runs on what is there, and a
``model_lane_uncertified_local`` event in the turn's Activity says which model is uncertified.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from adapters.base_adapter import ModelResponse
from core.memory_first_router import MemoryFirstRouter
from core.model_health import reset_provider_health
from core.runtime_task_events import register_runtime_event_sink, unregister_runtime_event_sink
from storage.db import get_connection
from storage.migrations import run_migrations
from storage.model_provider_manifest import ModelProviderManifest, upsert_provider_manifest
from tests._authorship_certification import certify_for_authorship

TOOL_WORK = "run the tests and fix the failing one"


def _local(model_name: str, parameter_billions: float) -> ModelProviderManifest:
    return ModelProviderManifest(
        provider_name="ollama-local",
        model_name=model_name,
        source_type="http",
        adapter_type="local_qwen_provider",
        license_name="Apache-2.0",
        license_reference="https://ollama.com/library/qwen3",
        weight_location="external",
        runtime_dependency="ollama",
        capabilities=["summarize", "format", "structured_json", "tool_intent"],
        runtime_config={"base_url": "http://127.0.0.1:11434"},
        metadata={"deployment_class": "local", "cost_class": "free_local", "parameter_billions": parameter_billions},
    )


@pytest.fixture
def clean_registry():
    run_migrations()
    reset_provider_health()
    try:
        from core.final_answer_authorship import reset_for_tests
    except ImportError:  # pragma: no cover - present on this branch
        reset_for_tests = None
    if reset_for_tests:
        reset_for_tests()
    conn = get_connection()
    try:
        for table in ("model_provider_manifests", "candidate_knowledge_lane", "local_tasks",
                      "local_model_tool_certification_runs"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                continue
        conn.commit()
    finally:
        conn.close()
    yield
    if reset_for_tests:
        reset_for_tests()


def _seed(*pairs: tuple[ModelProviderManifest, str]) -> None:
    for manifest, state in pairs:
        upsert_provider_manifest(manifest)
        certify_for_authorship(manifest, state=state)


def _run(ranked: list[ModelProviderManifest], *, stream: str, requested_model: str = "") -> tuple[list[str], list[dict], object]:
    router = MemoryFirstRouter()
    events: list[dict] = []
    register_runtime_event_sink(stream, events.append)
    built: list[str] = []
    adapter = mock.Mock()
    adapter.health_check.return_value = {"ok": True}
    adapter.supports_streaming.return_value = False
    adapter.estimate_cost_class.return_value = "free_local"
    adapter.get_license_metadata.return_value = {}
    adapter.run_text_task.return_value = ModelResponse(output_text='{"tool": "none"}', confidence=0.8)

    def build_adapter(manifest, *args, **kwargs):
        built.append(manifest.model_name)
        return adapter

    task = SimpleNamespace(task_id=f"{stream}-task", task_summary=TOOL_WORK)
    interpretation = SimpleNamespace(reconstructed_text=TOOL_WORK)
    report = SimpleNamespace(retrieval_confidence=0.2)
    report.to_dict = lambda: {"retrieval_confidence": 0.2, "external_evidence_attachments": []}
    context_result = SimpleNamespace(retrieval_confidence_score=0.2, report=report)
    source_context = {
        "runtime_event_stream_id": stream,
        "surface": "openclaw",
        "session_id": f"{stream}-session",
        "turn_id": f"{stream}-turn",
    }
    if requested_model:
        source_context["requested_model"] = requested_model
    try:
        with mock.patch("core.provider_routing._device_probe", return_value=None), mock.patch(
            "core.memory_first_router._cached_free_vram_gb", return_value=None
        ), mock.patch(
            "core.memory_first_router.rank_provider_candidates", return_value=list(ranked)
        ), mock.patch.object(router.registry, "build_adapter", side_effect=build_adapter):
            decision = router._execute_provider_task(
                task=task,
                classification={"task_class": "debugging"},
                interpretation=interpretation,
                context_result=context_result,
                persona=SimpleNamespace(),
                task_hash=f"{stream}-hash",
                task_kind="tool_intent",
                output_mode="tool_intent",
                allow_paid_fallback=False,
                provider_role="drone",
                surface="openclaw",
                source_context=source_context,
            )
    finally:
        unregister_runtime_event_sink(stream)
    return built, events, decision


def test_the_autopilot_still_picks_the_small_model_for_tool_work(clean_registry) -> None:
    """The pick itself is unchanged: it is a speed pick, and this fix does not touch it."""
    small, author = _local("qwen3:4b", 4.0), _local("qwen3:8b", 8.0)
    _seed((small, "degraded"), (author, "verified"))
    _built, _events, decision = _run([small, author], stream="cert-pick")
    assert decision.details["autopilot_plan"]["selected_model"] == "qwen3:4b"
    assert decision.details["autopilot_plan"]["lane"] == "tiny"


def test_tool_work_runs_on_the_certified_model_not_the_degraded_one(clean_registry) -> None:
    small, author = _local("qwen3:4b", 4.0), _local("qwen3:8b", 8.0)
    _seed((small, "degraded"), (author, "verified"))
    built, events, decision = _run([small, author], stream="cert-prefer")
    assert built and built[0] == "qwen3:8b", built
    assert decision.details["ranked_candidates"][0] == author.provider_id
    preferred = [e for e in events if e.get("event_type") == "model_lane_certified_preferred"]
    assert preferred, [e.get("event_type") for e in events]
    assert preferred[0].get("provider_id") == author.provider_id
    assert preferred[0].get("replaced_provider_id") == small.provider_id


def test_the_ranked_order_does_not_matter(clean_registry) -> None:
    small, author = _local("qwen3:4b", 4.0), _local("qwen3:8b", 8.0)
    _seed((small, "degraded"), (author, "verified"))
    built, _events, _decision = _run([author, small], stream="cert-order")
    assert built and built[0] == "qwen3:8b", built


def test_a_header_pin_to_the_uncertified_model_is_kept(clean_registry) -> None:
    small, author = _local("qwen3:4b", 4.0), _local("qwen3:8b", 8.0)
    _seed((small, "degraded"), (author, "verified"))
    built, events, _decision = _run([small, author], stream="cert-pin", requested_model=small.provider_id)
    assert built and built[0] == "qwen3:4b", built
    assert not [e for e in events if e.get("event_type") == "model_lane_certified_preferred"]


def test_with_no_certified_local_model_the_turn_runs_and_says_so(clean_registry) -> None:
    small = _local("qwen3:4b", 4.0)
    _seed((small, "degraded"))
    built, events, _decision = _run([small], stream="cert-none")
    assert built and built[0] == "qwen3:4b", built
    warned = [e for e in events if e.get("event_type") == "model_lane_uncertified_local"]
    assert warned, [e.get("event_type") for e in events]
    assert warned[0].get("provider_id") == small.provider_id
    assert "qwen3:4b" in str(warned[0].get("reason") or "")


def test_two_certified_models_keep_the_autopilot_pick(clean_registry) -> None:
    small, author = _local("qwen3:4b", 4.0), _local("qwen3:8b", 8.0)
    _seed((small, "verified"), (author, "verified"))
    built, events, _decision = _run([author, small], stream="cert-both")
    assert built and built[0] == "qwen3:4b", built
    assert not [e for e in events if e.get("event_type") in {"model_lane_certified_preferred",
                                                               "model_lane_uncertified_local"}]
