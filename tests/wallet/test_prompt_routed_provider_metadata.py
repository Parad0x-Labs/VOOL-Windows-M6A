"""The served wallet model fixture satisfies the real local metadata gate."""
from __future__ import annotations

import json
from urllib.request import Request, urlopen

import pytest

from adapters.base_adapter import ModelRequest
from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from core.local_ollama_inventory import ollama_reasoning_support
from storage.model_provider_manifest import ModelProviderManifest
from tests.wallet._rig_provider import CERTIFY_SEED, MODEL, PromptRoutedProvider


@pytest.mark.parametrize("path", ["/api/tags", "/api/ps"])
def test_scripted_inventory_carries_the_registered_digest(path):
    with PromptRoutedProvider() as provider:
        with urlopen(provider.base_url + path, timeout=5) as response:
            row, = json.load(response)["models"]
        assert row["name"] == row["model"] == MODEL
        assert row["digest"] == "sha256:stub"
        assert row["size"] == row["size_vram"] == 1
        assert provider.generations_for(MODEL) == 0


def test_scripted_weight_metadata_is_explicitly_not_real_weights():
    with PromptRoutedProvider() as provider:
        request = Request(provider.base_url + "/api/show", data=json.dumps({"model": MODEL}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            metadata = json.load(response)["model_info"]
        assert metadata == {"general.architecture": "scripted"}
        assert ollama_reasoning_support(metadata) == "unknown"
        assert provider.generations_for(MODEL) == 0


def test_real_adapter_accepts_the_scripted_identity_without_a_gate_override():
    with PromptRoutedProvider() as provider:
        manifest = ModelProviderManifest(
            provider_name="ollama-local", model_name=MODEL, source_type="http", adapter_type="openai_compatible",
            runtime_config={"base_url": provider.base_url},
            metadata={"runtime_family": "ollama", "model_digest": "sha256:stub"},
        )
        adapter = OpenAICompatibleAdapter(manifest)
        adapter._prepare_ollama_reasoning(ModelRequest(task_kind="chat", prompt="metadata proof"))
        identity = adapter.tool_certification_runtime_identity()
        assert identity["model_digest"] == "sha256:stub"
        assert identity["reasoning_support"] == "unknown"
        assert provider.generations_for(MODEL) == 0


@pytest.mark.parametrize("root", [r"C:\Users\tester\source", r"C:\Users\tester\project with spaces"])
def test_certification_seed_is_portable_python_source(root):
    code = CERTIFY_SEED.format(root=root, registered=[MODEL])
    assert compile(code, "wallet-certification-seed", "exec") is not None
