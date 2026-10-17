"""The Ollama OpenAI door must measure the same reasoning mode as serving."""

from __future__ import annotations

import pytest

from adapters.base_adapter import ModelRequest
from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from core.local_model_tool_certification import ADD_TOOL, _bounded_exchange_evidence, certification_fingerprint_payload
from tests.test_local_model_door_and_probe_measurement import _exchange, _ollama_manifest


def _adapter(*, think=False, model="qwen3:4b", family="ollama"):
    manifest = _ollama_manifest(
        model, think=think, api_path="/v1/chat/completions", context_window=8192
    )
    manifest.metadata["runtime_family"] = family
    return OpenAICompatibleAdapter(manifest)


@pytest.mark.parametrize("think,effort,budget", [(False, "none", 384), (True, "high", 2432)])
def test_certification_ollama_openai_honors_saved_mode(monkeypatch, think, effort, budget):
    adapter = _adapter(think=think)
    capture = _exchange(monkeypatch, adapter.manifest)
    payload = capture.payloads[0]["json"]
    assert payload["reasoning_effort"] == effort
    assert payload["max_tokens"] == budget
    assert "think" not in payload
    assert capture.payloads[0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"


@pytest.mark.parametrize(
    "think,mode,effort,budget",
    [(True, "disabled", "none", 384), (False, "required", "high", 2432),
     (False, "auto", "none", 384), (True, "auto", "high", 2432)],
)
def test_serving_uses_the_same_ollama_openai_policy(think, mode, effort, budget):
    payload = _adapter(think=think)._build_openai_payload(
        ModelRequest(task_kind="chat", prompt="hello", max_output_tokens=384, reasoning_mode=mode),
        force_json=False, stream=False,
    )
    assert payload["reasoning_effort"] == effort
    assert payload["max_tokens"] == budget


@pytest.mark.parametrize("family,model", [("openai-compatible", "qwen3:4b"), ("ollama", "qwen2.5:7b")])
def test_probe_does_not_add_ollama_thinking_control_to_other_lanes(monkeypatch, family, model):
    adapter = _adapter(model=model, family=family)
    payload = _exchange(monkeypatch, adapter.manifest).payloads[0]["json"]
    assert "reasoning_effort" not in payload
    assert payload["max_tokens"] == 384


def test_enabled_reasoning_budget_respects_the_context_window(monkeypatch):
    adapter = _adapter(think=True)
    adapter.manifest.runtime_config["context_window"] = 2048
    payload = _exchange(monkeypatch, adapter.manifest).payloads[0]["json"]
    assert payload["max_tokens"] == 1024


def test_ollama_openai_policy_has_a_distinct_certification_fingerprint():
    changed = certification_fingerprint_payload(_adapter().manifest)
    native = _ollama_manifest("qwen3:4b")
    unchanged = certification_fingerprint_payload(native)
    assert changed["adapter_version"] != unchanged["adapter_version"]


def test_probe_seal_and_receipt_name_the_effective_wire_budget(monkeypatch):
    captured = {}

    class Permit:
        def consume(self):
            return captured["payload"]

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"tool_calls": []}, "finish_reason": "length"}]}

    def seal(**kwargs):
        captured.update(kwargs)
        return Permit()

    monkeypatch.setattr("adapters.openai_compatible_adapter.seal_direct_provider_invocation", seal)
    monkeypatch.setattr("adapters.openai_compatible_adapter.requests.post", lambda *a, **kw: Response())
    result = _adapter(think=True).tool_certification_exchange(
        messages=[{"role": "user", "content": "probe"}], tools=(ADD_TOOL,),
        tool_choice="required", max_output_tokens=384, timeout_seconds=3,
    )
    assert captured["max_output_tokens"] == captured["payload"]["max_tokens"] == 2432
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["tools"][0]["function"]["name"] == "vool_probe_add"
    evidence = _bounded_exchange_evidence("model_emission", result)
    assert evidence["effective_max_output_tokens"] == 2432
    assert evidence["reasoning_effort"] == "high"
    assert evidence["finish_reason"] == "length"
    assert evidence["raw_body_persisted"] is False


def test_unknown_wire_budget_is_not_reported_as_zero():
    evidence = _bounded_exchange_evidence("model_emission", {"body": {}, "status_code": 200})
    assert evidence["effective_max_output_tokens"] is None
