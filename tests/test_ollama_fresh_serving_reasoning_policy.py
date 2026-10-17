"""Fresh serving adapters measure local weight policy before any generation."""
from __future__ import annotations

import json

import pytest

from adapters.base_adapter import ModelRequest
from adapters.openai_compatible_adapter import (
    LocalModelIdentityChangedError,
    LocalModelMetadataUnavailableError,
    OpenAICompatibleAdapter,
    UnsupportedLocalReasoningPolicyError,
)
from core.provider_call_deadline import PROVIDER_DEADLINE_KEY, ProviderCallDeadlineExceededError
from tests.test_local_model_door_and_probe_measurement import _ollama_manifest


class Response:
    ok = True
    status_code = 200
    headers = {}
    encoding = "utf-8"

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def iter_lines(self, **kwargs):
        if "choices" in self.body:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
            yield "data: [DONE]"
        else:
            yield json.dumps({**self.body, "done": True})


def backend(monkeypatch, *, finetune="Instruct", api_path="", digest="current-digest"):
    manifest = _ollama_manifest("alias:nothink", think=False, api_path=api_path)
    manifest.metadata["model_digest"] = digest
    adapter = OpenAICompatibleAdapter(manifest)
    state = {"digest": digest, "finetune": finetune, "show_error": False, "replace_during_show": False,
             "show_status": True, "missing_metadata": False, "change_binding": False, "get_error": False}
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert url == "http://127.0.0.1:11434/api/tags"
        if state["get_error"]:
            raise OSError("synthetic unavailable backend")
        return Response({"models": [{"name": adapter.manifest.model_name, "digest": state["digest"]}]})

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/show"):
            assert kwargs["json"] == {"model": adapter.manifest.model_name}
            if state["show_error"]:
                raise OSError("synthetic unavailable backend")
            if state["replace_during_show"]:
                state["digest"] = "replaced-during-show"
            if state["change_binding"]:
                adapter.manifest.runtime_config["base_url"] = "http://127.0.0.1:22434"
            response = Response({"model_info": {
                "general.architecture": "qwen3", "general.version": "2507",
                "general.finetune": state["finetune"],
            }} if not state["missing_metadata"] else {})
            response.ok = state["show_status"]
            return response
        if api_path:
            return Response({"model": adapter.manifest.model_name, "choices": [
                {"message": {"content": '{"ok":true}'}, "finish_reason": "stop"},
            ]})
        return Response({"model": adapter.manifest.model_name, "message": {"content": '{"ok":true}'}, "done_reason": "stop"})

    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.get", get)
    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.post", post)
    monkeypatch.setattr(adapter, "_gate_local_model_load", lambda *a, **kw: None)
    monkeypatch.setattr(adapter, "_record_benchmark_best_effort", lambda *a, **kw: None)
    return adapter, state, calls


def serve(adapter, request, entrypoint):
    result = getattr(adapter, entrypoint)(request)
    return list(result) if entrypoint == "stream_text_task" else result


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
@pytest.mark.parametrize("entrypoint", ["run_text_task", "run_structured_task", "invoke", "stream_text_task"])
@pytest.mark.parametrize("finetune", ["Thinking", "Instruct"])
def test_every_fresh_public_dispatch_measures_before_generation(monkeypatch, api_path, entrypoint, finetune):
    adapter, _, calls = backend(monkeypatch, finetune=finetune, api_path=api_path)
    request = ModelRequest(task_kind="chat", prompt="hello", max_output_tokens=384, reasoning_mode="disabled")
    if finetune == "Thinking":
        with pytest.raises(UnsupportedLocalReasoningPolicyError):
            serve(adapter, request, entrypoint)
        assert len(calls) == 3
    else:
        result = serve(adapter, request, entrypoint)
        assert result
        assert len(calls) == 4
        payload = calls[-1][1]["json"]
        assert "think" not in payload and "reasoning_effort" not in payload
        assert (payload["max_tokens"] if api_path else payload["options"]["num_predict"]) == 384
    assert [url.rsplit("/", 1)[-1] for url, _ in calls[:3]] == ["tags", "show", "tags"]


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
def test_fresh_non_thinking_model_refuses_required_reasoning(monkeypatch, api_path):
    adapter, _, calls = backend(monkeypatch, api_path=api_path)
    request = ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="required")
    with pytest.raises(UnsupportedLocalReasoningPolicyError):
        adapter.run_text_task(request)
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["changed_before", "changed_during", "changed_binding", "show_unavailable", "show_refused", "tags_unavailable", "missing_digest", "unknown_digest", "missing_metadata"])
def test_discovery_failure_clears_previous_observation_and_never_generates(monkeypatch, failure):
    adapter, state, calls = backend(monkeypatch)
    request = ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="disabled")
    adapter.run_text_task(request)
    assert adapter._measured_ollama_reasoning_support() == "non_thinking_only"
    calls.clear()
    if failure == "changed_before":
        state["digest"] = "replacement"
    elif failure == "changed_during":
        state["replace_during_show"] = True
    elif failure == "show_unavailable":
        state["show_error"] = True
    elif failure == "show_refused":
        state["show_status"] = False
    elif failure == "tags_unavailable":
        state["get_error"] = True
    elif failure == "changed_binding":
        state["change_binding"] = True
    elif failure == "missing_digest":
        state["digest"] = ""
    elif failure == "unknown_digest":
        state["digest"] = "unknown"
    else:
        state["missing_metadata"] = True
    error = LocalModelIdentityChangedError if failure.startswith("changed") else LocalModelMetadataUnavailableError
    with pytest.raises(error):
        adapter.run_text_task(request)
    assert adapter._measured_ollama_reasoning_support() == "unknown"
    assert all(url.endswith(("/api/tags", "/api/show")) for url, _ in calls)


def test_unrecognized_but_available_weights_retain_legacy_policy(monkeypatch):
    adapter, _, calls = backend(monkeypatch, finetune="unknown")
    adapter.run_text_task(ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="disabled"))
    assert adapter._measured_ollama_reasoning_support() == "unknown"
    assert calls[-1][1]["json"]["think"] is False  # Unknown is not an invented capability.


@pytest.mark.parametrize("cancelled", [False, True])
def test_expired_or_cancelled_request_sends_no_metadata(monkeypatch, cancelled):
    adapter, _, calls = backend(monkeypatch)
    monkeypatch.setattr("core.runtime_active_clock.monotonic", lambda: 100.0)
    request = ModelRequest(task_kind="chat", prompt="hello", metadata={PROVIDER_DEADLINE_KEY: 99.0}, cancel_check=lambda: cancelled)
    error = RuntimeError if cancelled else ProviderCallDeadlineExceededError
    with pytest.raises(error):
        adapter.run_text_task(request)
    assert calls == []


def test_metadata_transfers_are_clamped_to_parent_deadline(monkeypatch):
    adapter, _, calls = backend(monkeypatch)
    monkeypatch.setattr("core.runtime_active_clock.monotonic", lambda: 100.0)
    request = ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="disabled", metadata={PROVIDER_DEADLINE_KEY: 100.25})
    adapter.run_text_task(request)
    assert all(kwargs["timeout"] == (0.25, 0.25) for _, kwargs in calls[:3])


@pytest.mark.parametrize("api_path", ["", "/v1/chat/completions"])
@pytest.mark.parametrize("entrypoint", ["run_text_task", "stream_text_task"])
def test_fresh_thinking_only_weights_can_serve_required_reasoning(monkeypatch, api_path, entrypoint):
    adapter, _, calls = backend(monkeypatch, finetune="Thinking", api_path=api_path)
    result = serve(adapter, ModelRequest(task_kind="chat", prompt="hello", reasoning_mode="required", max_output_tokens=384), entrypoint)
    assert result and len(calls) == 4
    payload = calls[-1][1]["json"]
    if api_path:
        assert payload["reasoning_effort"] == "high" and payload["max_tokens"] == 2432
    else:
        assert payload["think"] is True and payload["options"]["num_predict"] == 2432


@pytest.mark.parametrize("family,base_url", [("openai-compatible", "http://127.0.0.1:11434"), ("ollama", "https://example.invalid/v1"), ("ollama", "http://0.0.0.0:11434")])
def test_discovery_introduces_no_cloud_or_wildcard_metadata_reads(monkeypatch, family, base_url):
    adapter, _, calls = backend(monkeypatch)
    adapter.manifest.metadata["runtime_family"] = family
    adapter.manifest.runtime_config["base_url"] = base_url
    adapter._prepare_ollama_reasoning(ModelRequest(task_kind="chat", prompt="hello"))
    assert calls == []


@pytest.mark.parametrize("finetune", ["Thinking", "Instruct"])
@pytest.mark.parametrize("strategy", ["ollama_chat", "ollama_generate"])
def test_prewarm_uses_fresh_discovery_and_preserves_error_contract(monkeypatch, finetune, strategy):
    adapter, _, calls = backend(monkeypatch, finetune=finetune)
    adapter.manifest.runtime_config["prewarm"] = {"strategy": strategy, "timeout_seconds": 2.0}
    result = adapter.prewarm()
    if finetune == "Thinking":
        assert result["ok"] is False and result["status"] == "error"
        assert "thinking cannot be disabled" in result["error"]
        assert len(calls) == 3
    else:
        assert result["ok"] is True and result["status"] == "prewarmed"
        assert len(calls) == 4
        assert "think" not in calls[-1][1]["json"]


def test_deadline_expiring_during_discovery_prevents_show_and_generation(monkeypatch):
    adapter, _, calls = backend(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("core.runtime_active_clock.monotonic", lambda: clock[0])
    from adapters.openai_compatible_adapter import requests

    original_get = requests.get

    def slow_get(*args, **kwargs):
        response = original_get(*args, **kwargs)
        clock[0] = 101.0
        return response

    monkeypatch.setattr(requests, "get", slow_get)
    with pytest.raises(ProviderCallDeadlineExceededError):
        adapter.run_text_task(ModelRequest(task_kind="chat", prompt="hello", metadata={PROVIDER_DEADLINE_KEY: 100.25}))
    assert len(calls) == 1 and calls[0][0].endswith("/api/tags")


def test_prewarm_preserves_configured_timeout_but_clamps_generation_budget(monkeypatch):
    adapter, _, calls = backend(monkeypatch)
    adapter.manifest.runtime_config["prewarm"] = {"strategy": "ollama_chat", "timeout_seconds": 2.0}
    clock = [100.0]
    monkeypatch.setattr("core.runtime_active_clock.monotonic", lambda: clock[0])
    from adapters.openai_compatible_adapter import requests

    original_post = requests.post

    def delayed_post(url, **kwargs):
        response = original_post(url, **kwargs)
        if url.endswith("/api/show"):
            clock[0] = 100.5
            return response
        raise requests.exceptions.ReadTimeout("synthetic cold-start timeout")

    monkeypatch.setattr(requests, "post", delayed_post)
    result = adapter.prewarm()
    assert result["ok"] is True and result["status"] == "timed_out"
    assert result["timeout_seconds"] == 2.0
    assert calls[-1][1]["timeout"] == 1.5


def test_manifest_storage_failure_still_prevents_generation_after_metadata(monkeypatch):
    adapter, _, calls = backend(monkeypatch)

    def failed_seal(**kwargs):
        raise RuntimeError("manifest_store_failed")

    monkeypatch.setattr("adapters.openai_compatible_adapter.seal_provider_invocation", failed_seal)
    with pytest.raises(RuntimeError, match="manifest_store_failed"):
        adapter.run_text_task(ModelRequest(task_kind="chat", prompt="private-context-marker"))
    assert len(calls) == 3  # Metadata only; no prompt-bearing or generative transfer.
    assert all(url.endswith(("/api/tags", "/api/show")) for url, _ in calls)
    assert calls[1][1]["json"] == {"model": adapter.manifest.model_name}
