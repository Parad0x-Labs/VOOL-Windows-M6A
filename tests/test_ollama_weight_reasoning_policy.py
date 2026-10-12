"""Weight metadata, not model aliases, decides which reasoning switches are real."""

from __future__ import annotations

import pytest

from adapters.base_adapter import ModelRequest
from adapters.openai_compatible_adapter import OpenAICompatibleAdapter, UnsupportedLocalReasoningPolicyError
from core.local_model_tool_certification import (
    certification_fingerprint_payload,
    certification_history,
    certification_status,
    run_local_model_tool_certification,
)
from tests.test_local_model_door_and_probe_measurement import _ollama_manifest
from tests.test_local_model_tool_certification import ProbeExchange


def _observed_adapter(monkeypatch, *, finetune="Thinking", think=False, api_path="", model="qwen3:4b"):
    adapter = OpenAICompatibleAdapter(_ollama_manifest(model, think=think, api_path=api_path))
    calls = []
    show = {
        "template": "synthetic template; not a capability declaration",
        "model_info": {
            "general.architecture": "qwen3",
            "general.version": "2507",
            "general.finetune": finetune,
        },
    }

    class Response:
        ok = True
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert url.endswith("/api/tags")
        return Response({"models": [{"name": model, "digest": "measured-weight-digest"}]})

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/show"):
            assert kwargs["json"] == {"model": adapter.manifest.model_name}
            return Response(show)
        return Response({"message": {"content": "", "tool_calls": []}})

    monkeypatch.setattr(OpenAICompatibleAdapter, "tool_certification_backend_version", lambda self: "0.34.0")
    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.get", get)
    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.post", post)
    identity = adapter.tool_certification_runtime_identity()
    return adapter, identity, calls, show


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
def test_thinking_only_off_is_refused_before_any_generation(monkeypatch, tmp_path, api_path):
    adapter, identity, calls, _ = _observed_adapter(monkeypatch, api_path=api_path)
    monkeypatch.setattr("core.model_registry.ModelRegistry.build_adapter", lambda _registry, _manifest: adapter)
    result = run_local_model_tool_certification(adapter.manifest, db_path=tmp_path / "cert.db")
    assert identity["reasoning_support"] == "thinking_only"
    assert result["state"] == "degraded"
    assert result["successful"] is False
    assert result["evidence"]["preflight"]["failure_code"] == "unsupported_reasoning_policy"
    assert result["evidence"]["exchange_count"] == 0
    assert result["stages"]["transport_acceptance"]["failure_code"] == "unsupported_reasoning_policy"
    assert "thinking cannot be disabled" in result["stages"]["transport_acceptance"]["detail"]
    assert all(url.endswith(("/api/tags", "/api/show")) for url, _ in calls)
    assert len(certification_history(db_path=tmp_path / "cert.db")) == 1
    assert result["fingerprint_components"]["reasoning_support"] == "thinking_only"


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_observed_thinking_only_model_cannot_silently_disable_serving(monkeypatch, api_path, stream):
    adapter, _, calls, _ = _observed_adapter(monkeypatch, api_path=api_path, model="alias:nothink")
    request = ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="disabled", max_output_tokens=384)
    builder = adapter._build_openai_payload if api_path else adapter._build_ollama_payload
    with pytest.raises(UnsupportedLocalReasoningPolicyError, match="thinking cannot be disabled"):
        builder(request, force_json=False, stream=stream)
    assert adapter._is_thinking_capable_model() is True
    assert len(calls) == 2  # Metadata only, no serving or retry.


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
def test_observed_thinking_only_on_keeps_existing_bounded_probe_policy(monkeypatch, api_path):
    adapter, _, calls, _ = _observed_adapter(monkeypatch, think=True, api_path=api_path)
    adapter.tool_certification_exchange(
        messages=[{"role": "user", "content": "synthetic probe"}], tools=(),
        tool_choice=None, max_output_tokens=384, timeout_seconds=2,
    )
    payload = calls[-1][1]["json"]
    if api_path:
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == 2432
    else:
        assert payload["think"] is True
        assert payload["options"]["num_predict"] == 2432


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
@pytest.mark.parametrize("think", [False, True])
def test_observed_non_thinking_weights_omit_controls_and_reserve(monkeypatch, api_path, think):
    adapter, identity, calls, _ = _observed_adapter(monkeypatch, finetune="Instruct", think=think, api_path=api_path)
    assert identity["reasoning_support"] == "non_thinking_only"
    request = ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="disabled", max_output_tokens=384)
    builder = adapter._build_openai_payload if api_path else adapter._build_ollama_payload
    payload = builder(request, force_json=False, stream=False)
    assert "think" not in payload and "reasoning_effort" not in payload
    assert (payload["max_tokens"] if api_path else payload["options"]["num_predict"]) == 384
    adapter.tool_certification_exchange(
        messages=[{"role": "user", "content": "synthetic probe"}], tools=(),
        tool_choice=None, max_output_tokens=384, timeout_seconds=2,
    )
    probe = calls[-1][1]["json"]
    assert "think" not in probe and "reasoning_effort" not in probe
    assert (probe["max_tokens"] if api_path else probe["options"]["num_predict"]) == 384
    request.reasoning_mode = "required"
    with pytest.raises(UnsupportedLocalReasoningPolicyError, match="required reasoning is unsupported"):
        builder(request, force_json=False, stream=False)


def test_new_metadata_read_does_not_reuse_old_variant(monkeypatch):
    adapter, identity, _, show = _observed_adapter(monkeypatch)
    assert identity["reasoning_support"] == "thinking_only"
    show["model_info"] = None
    assert adapter.tool_certification_runtime_identity()["reasoning_support"] == "unknown"
    assert adapter._measured_ollama_reasoning_support() == "unknown"
    assert adapter._ollama_think_flag() is False  # Legacy behaviour, not a claim of measured support.


@pytest.mark.parametrize("change", ["model", "endpoint", "family", "digest"])
def test_observation_cannot_leak_to_a_different_binding(monkeypatch, change):
    adapter, _, _, _ = _observed_adapter(monkeypatch)
    if change == "model":
        adapter.manifest.model_name = "different:4b"
    elif change == "endpoint":
        adapter.manifest.runtime_config["base_url"] = "http://127.0.0.1:22434"
    elif change == "family":
        adapter.manifest.metadata["runtime_family"] = "openai-compatible"
    else:
        adapter.manifest.metadata["model_digest"] = "replacement-weight-digest"
    assert adapter._measured_ollama_reasoning_support() == "unknown"


def test_fingerprint_changes_with_observed_variant_and_saved_mode(monkeypatch):
    adapter, identity, _, _ = _observed_adapter(monkeypatch)
    original = certification_fingerprint_payload(adapter.manifest, runtime_identity=identity)
    changed = certification_fingerprint_payload(
        adapter.manifest, runtime_identity={**identity, "reasoning_support": "non_thinking_only"},
    )
    assert original != changed
    adapter.manifest.runtime_config["think"] = True
    enabled = adapter.tool_certification_runtime_identity()
    assert certification_fingerprint_payload(adapter.manifest, runtime_identity=enabled) != original
    assert "synthetic template" not in str(identity)


def test_non_thinking_probe_still_requires_the_full_sealed_contract(monkeypatch, tmp_path):
    adapter, _, calls, _ = _observed_adapter(monkeypatch, finetune="Instruct")
    exchange = ProbeExchange()
    monkeypatch.setattr(adapter, "tool_certification_exchange", exchange)
    monkeypatch.setattr("core.model_registry.ModelRegistry.build_adapter", lambda _registry, _manifest: adapter)
    result = run_local_model_tool_certification(adapter.manifest, db_path=tmp_path / "cert.db")
    assert result["state"] == "verified"
    assert result["successful"] is True
    assert all(stage["state"] == "passed" for stage in result["stages"].values())
    assert result["evidence"]["exchange_count"] == len(exchange.calls) == 4
    assert all(url.endswith(("/api/tags", "/api/show")) for url, _ in calls)
    assert certification_status(adapter.manifest, db_path=tmp_path / "cert.db")["state"] == "verified"


def test_changed_variant_or_saved_mode_invalidates_previously_verified_evidence(monkeypatch, tmp_path):
    adapter, _, _, show = _observed_adapter(monkeypatch, finetune="Instruct")
    monkeypatch.setattr(adapter, "tool_certification_exchange", ProbeExchange())
    monkeypatch.setattr("core.model_registry.ModelRegistry.build_adapter", lambda _registry, _manifest: adapter)
    run_local_model_tool_certification(adapter.manifest, db_path=tmp_path / "cert.db")
    show["model_info"]["general.finetune"] = "Thinking"
    assert certification_status(adapter.manifest, db_path=tmp_path / "cert.db")["state"] == "stale"
    show["model_info"]["general.finetune"] = "Instruct"
    adapter.manifest.runtime_config["think"] = True
    assert certification_status(adapter.manifest, db_path=tmp_path / "cert.db")["state"] == "stale"


def test_failed_refresh_clears_previous_variant_observation(monkeypatch):
    adapter, _, _, _ = _observed_adapter(monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError("synthetic unavailable backend")

    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.post", unavailable)
    assert adapter.tool_certification_runtime_identity()["reasoning_support"] == "unknown"
    assert adapter._measured_ollama_reasoning_support() == "unknown"
