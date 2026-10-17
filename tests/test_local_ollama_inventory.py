from __future__ import annotations

from unittest import mock

import pytest

from core.local_ollama_inventory import installed_ollama_model_inventory, ollama_reasoning_support


@pytest.mark.parametrize("architecture", ["qwen3", "qwen3moe"])
@pytest.mark.parametrize("finetune,expected", [("Thinking", "thinking_only"), ("Instruct", "non_thinking_only")])
def test_reasoning_support_uses_actual_2507_weight_metadata(architecture, finetune, expected):
    assert ollama_reasoning_support({
        "general.architecture": architecture,
        "general.version": "2507",
        "general.finetune": finetune,
    }) == expected


@pytest.mark.parametrize("metadata", [
    None, [], {}, {"general.name": "Qwen3-4B-Thinking-2507"},
    {"general.architecture": "qwen3", "general.finetune": "Thinking"},
    {"general.architecture": "qwen3", "general.version": "2507", "general.finetune": "hybrid"},
    {"general.architecture": "other", "general.version": "2507", "general.finetune": "Thinking"},
])
def test_reasoning_support_does_not_guess_from_missing_or_unknown_metadata(metadata):
    assert ollama_reasoning_support(metadata) == "unknown"


def test_inventory_preserves_exact_ollama_artifact_size_bytes() -> None:
    payload = [
        {"name": "qwen2.5:7b", "size": 4_683_087_332},
        {"name": "vool-qwen3-30b-a3b:nothink", "size": 18_556_698_002},
    ]
    with mock.patch("core.local_ollama_inventory._ollama_models_payload", return_value=payload):
        inventory = installed_ollama_model_inventory(env={})

    assert [(item.name, item.size_bytes) for item in inventory] == [
        ("qwen2.5:7b", 4_683_087_332),
        ("vool-qwen3-30b-a3b:nothink", 18_556_698_002),
    ]


def test_explicit_name_only_inventory_does_not_fabricate_measured_bytes() -> None:
    inventory = installed_ollama_model_inventory(
        env={"VOOL_INSTALLED_OLLAMA_MODELS": "qwen2.5:7b,vool-qwen3-30b-a3b:nothink"}
    )

    assert [(item.name, item.size_bytes) for item in inventory] == [
        ("qwen2.5:7b", 0),
        ("vool-qwen3-30b-a3b:nothink", 0),
    ]
