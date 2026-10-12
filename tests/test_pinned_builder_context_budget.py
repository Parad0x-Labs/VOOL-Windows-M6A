"""A pinned local build must leave room for its protected input, not reserve the whole runner."""
from types import SimpleNamespace
from unittest import mock

import pytest

from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from core.agent_runtime.builder.controller import BuilderGenerationBudget
from core.agent_runtime.builder.pinned_generation import build_pinned_generate_fn
from core.prompt_budget import estimate_message_tokens


def _generate(*, context_window, prompt="Write the requested canary text only.", openai=False,
              metadata_context=False, reasoning_retry=False, family="ollama"):
    metadata = {"runtime_family": family}
    config = {"base_url": "http://127.0.0.1:11434"}
    (metadata if metadata_context else config)["context_window"] = context_window
    if openai:
        config["api_path"] = "/v1/chat/completions"
    manifest = SimpleNamespace(provider_id="ollama-local:builder-test", model_name="builder-test",
                               metadata=metadata, runtime_config=config)
    adapter = OpenAICompatibleAdapter(manifest)
    requests, payloads = [], []

    def invoke(**kwargs):
        request = kwargs["request"]
        requests.append(request)
        if reasoning_retry and len(requests) == 1:
            return adapter, None, "Reasoning is mandatory for this endpoint and cannot be disabled."
        payload = (adapter._build_openai_payload if openai else adapter._build_ollama_payload)(
            request, force_json=False, stream=False)
        payloads.append(payload)
        return adapter, SimpleNamespace(output_text="M6B_SYNTHETIC_CANARY"), None

    budget = BuilderGenerationBudget()
    generate = build_pinned_generate_fn(
        SimpleNamespace(memory_router=SimpleNamespace(_invoke_manifest=invoke)), manifest=manifest,
        task=SimpleNamespace(task_id="context-budget-test"), source_context={}, budget=budget)
    with mock.patch.object(adapter, "_measured_ollama_reasoning_support", return_value="non_thinking_only"):
        output = generate(prompt)
    return output, requests, payloads, budget


@pytest.mark.parametrize("openai", [False, True])
@pytest.mark.parametrize("context_window", [512, 4096, 6144, 16384])
def test_pinned_builder_fits_real_local_wire_without_losing_input(openai, context_window):
    prompt = "Write the requested canary text only."
    output, requests, payloads, _ = _generate(context_window=context_window, prompt=prompt, openai=openai)
    assert output == "M6B_SYNTHETIC_CANARY"
    request, payload = requests[-1], payloads[-1]
    tokens = estimate_message_tokens(payload["messages"])
    ceiling = payload["max_tokens"] if openai else payload["options"]["num_predict"]
    assert ceiling == min(6144, context_window - tokens)
    assert 0 < ceiling <= context_window - tokens
    assert payload["options"]["num_ctx"] == context_window
    assert payload["messages"] == [{"role": "system", "content": request.system_prompt},
                                   {"role": "user", "content": prompt}]
    assert request.metadata["prompt_budget"]["system_prompt_preserved"] is True
    assert request.metadata["prompt_budget"]["status"] == "fit"


def test_pinned_builder_honours_metadata_context_window():
    output, _, payloads, _ = _generate(context_window=6144, metadata_context=True)
    assert output == "M6B_SYNTHETIC_CANARY"
    assert payloads[0]["options"]["num_ctx"] == 6144


@pytest.mark.parametrize("openai", [False, True])
def test_pinned_builder_oversize_input_still_fails_closed(openai):
    output, _, payloads, budget = _generate(context_window=64, prompt="x " * 1000, openai=openai)
    assert output == ""
    assert payloads == []
    assert budget.last_error == "PromptBudgetExceededError"


@pytest.mark.parametrize("openai", [False, True])
def test_mandatory_reasoning_retry_uses_same_local_context_limit(openai):
    output, requests, payloads, budget = _generate(context_window=6144, openai=openai, reasoning_retry=True)
    assert output == "M6B_SYNTHETIC_CANARY"
    assert len(requests) == budget.calls == 2
    assert requests[1].metadata["reasoning_mandated_by_provider"] is True
    payload = payloads[0]
    ceiling = payload["max_tokens"] if openai else payload["options"]["num_predict"]
    assert ceiling + estimate_message_tokens(payload["messages"]) <= 6144


@pytest.mark.parametrize("context_window", [0, "unknown"])
def test_unknown_local_context_keeps_existing_output_limit(context_window):
    output, requests, _, _ = _generate(context_window=context_window)
    assert output == "M6B_SYNTHETIC_CANARY"
    assert requests[0].max_output_tokens == 6144


def test_cloud_builder_keeps_existing_output_policy():
    output, requests, _, _ = _generate(context_window=6144, family="openai", openai=True)
    assert output == "M6B_SYNTHETIC_CANARY"
    assert requests[0].max_output_tokens == 6144
