"""An 8 GB graphics card runs qwen3:8b; the per-turn hardware-fit gate must not refuse it.

Measured on Windows 11, RTX 4060 8 GB (8188 MiB), i5-14400F, 32 GB RAM, build a37d6fd2:

* Setup downloaded qwen3:8b. Ollama's /api/tags reports 5,225,388,164 bytes (4.87 GiB, Q4_K_M).
* Certification ran on the GPU and returned "verified" in 36.8 s.
* The first chat turn was then refused: "`qwen3:8b` needs about 7.5 GB of graphics memory and this
  computer's graphics card can spare about 6.8 GB for it, so I didn't start it." The routing log
  said "needs 7.50 GB, this host allows 6.80 GB": the parameter-count estimate (8 x 0.75 + 1.5),
  not the measured size (4.87 + 1.5 = 6.37 GB). The lane had no ``artifact_size_gb``.

Why the lane had no measured size: boot stamps it once, in the deferred prewarm, from whatever
Ollama lists at that moment, and Setup's click-to-download registered the lane after its pull
without stamping it. A model downloaded from Setup was judged by the estimate until VOOL restarted.

Now Setup's pull stamps the lane it registers, and the per-turn gate itself reads Ollama's size
for a local lane it is about to refuse on an unmeasured estimate, so boot and every turn agree.
A model that is too big even by its measured size is still refused, and the refusal on a graphics
card names a local route (a smaller local model) as well as the cloud one.

LABELLED: virtual hosts (patched ``_device_probe``), a loopback fake Ollama, real manifests and the
real ranking gate.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core import provider_routing
from core.model_registry import ModelRegistry
from core.provider_routing import (
    local_hardware_shortfall,
    provider_capability_truth_for_manifest,
    rank_provider_candidates,
)
from core.runtime_provider_defaults import register_installed_local_model
from tests.test_local_model_memory_fit import QWEN3_8B_BYTES, _FakeOllama, home

STEVE_VRAM_GB = 8188 / 1024.0  # RTX 4060, 8188 MiB
STEVE_RAM_GB = 32.0
QWEN3_8B_GIB = QWEN3_8B_BYTES / 1024.0**3  # 4.87
QWEN3_14B_BYTES = 9_276_198_565  # Ollama's /api/tags size for qwen3:14b (8.6 GiB)


class _Execution:
    source = "no_provider_available"
    details = {}


def _gpu_host(monkeypatch, *, ram_gb: float, vram_gb: float, accelerator: str = "cuda") -> None:
    probe = SimpleNamespace(ram_gb=ram_gb, vram_gb=vram_gb, accelerator=accelerator)
    monkeypatch.setattr(provider_routing, "_device_probe", lambda: probe)


def _ranked_ids(registry: ModelRegistry) -> list[str]:
    return [
        manifest.provider_id
        for manifest in rank_provider_candidates(
            registry, task_kind="summarize", output_mode="text", enforce_hardware_fit=True
        )
    ]


def test_the_numbers_match_steves_machine() -> None:
    # 8188 MiB minus the 1.2 GB the gate keeps for the desktop: the 6.8 GB in the refusal.
    assert round(STEVE_VRAM_GB - 1.2, 1) == 6.8
    assert abs(QWEN3_8B_GIB - 4.87) < 0.01
    assert provider_routing.estimated_local_model_resident_gb("qwen3:8b") == pytest.approx(7.5)
    assert provider_routing.estimated_local_model_resident_gb(
        "qwen3:8b", artifact_size_gb=QWEN3_8B_GIB
    ) == pytest.approx(6.37, abs=0.01)


@pytest.mark.parametrize("accelerator", ["cuda", "directml"])
def test_an_unmeasured_lane_is_sized_by_the_turn_and_allowed(home, monkeypatch, caplog, accelerator) -> None:
    """Steve's state: the lane exists, Ollama is up and lists the model, nothing stamped its size."""
    _gpu_host(monkeypatch, ram_gb=STEVE_RAM_GB, vram_gb=STEVE_VRAM_GB, accelerator=accelerator)
    registry = ModelRegistry()
    assert register_installed_local_model(registry, model_tag="qwen3:8b") is not None
    assert "artifact_size_gb" not in (registry.get_manifest("ollama-local", "qwen3:8b").metadata or {})

    with _FakeOllama([{"name": "qwen3:8b", "size": QWEN3_8B_BYTES}]) as fake:
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        with caplog.at_level(logging.INFO, logger="core.provider_routing"):
            ranked = _ranked_ids(registry)

    assert "ollama-local:qwen3:8b" in ranked
    assert not [record for record in caplog.records if "left out of this turn" in record.getMessage()]
    manifest = registry.get_manifest("ollama-local", "qwen3:8b")
    assert manifest.metadata["artifact_size_gb"] == pytest.approx(QWEN3_8B_GIB, abs=0.001)
    capability = provider_capability_truth_for_manifest(manifest)
    assert capability.vram_budget_gb == pytest.approx(6.37, abs=0.01)
    assert capability.hardware_fit is True and capability.hardware_fit_reason == ""
    # Nothing to refuse, so the turn carries no shortfall sentence.
    assert local_hardware_shortfall() == {}


def test_setups_download_stamps_the_lane_it_registers(home, monkeypatch) -> None:
    """Setup's click-to-download registers the lane after the pull; it now records the size too."""
    from core import local_model_pull

    _gpu_host(monkeypatch, ram_gb=STEVE_RAM_GB, vram_gb=STEVE_VRAM_GB)
    with _FakeOllama([{"name": "qwen3:8b", "size": QWEN3_8B_BYTES}]) as fake:
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        local_model_pull._register("qwen3:8b")

    manifest = ModelRegistry().get_manifest("ollama-local", "qwen3:8b")
    assert manifest.metadata["artifact_size_gb"] == pytest.approx(QWEN3_8B_GIB, abs=0.001)
    assert provider_capability_truth_for_manifest(manifest).hardware_fit is True


def test_an_already_measured_lane_needs_no_ollama_call(home, monkeypatch) -> None:
    _gpu_host(monkeypatch, ram_gb=STEVE_RAM_GB, vram_gb=STEVE_VRAM_GB)
    registry = ModelRegistry()
    register_installed_local_model(registry, model_tag="qwen3:8b")
    manifest = registry.get_manifest("ollama-local", "qwen3:8b")
    registry.register_manifest(
        manifest.model_copy(update={"metadata": {**manifest.metadata, "artifact_size_gb": round(QWEN3_8B_GIB, 3)}})
    )
    calls: list[object] = []
    monkeypatch.setattr(
        "core.local_ollama_inventory.installed_ollama_model_inventory",
        lambda **kwargs: calls.append(kwargs) or (),
    )

    assert "ollama-local:qwen3:8b" in _ranked_ids(registry)
    assert calls == []


def test_without_ollamas_size_the_estimate_still_refuses(home, monkeypatch, caplog) -> None:
    """Negative: Ollama does not answer, so nothing measured the model and the 7.5 GB guess stands."""
    _gpu_host(monkeypatch, ram_gb=STEVE_RAM_GB, vram_gb=STEVE_VRAM_GB)
    registry = ModelRegistry()
    register_installed_local_model(registry, model_tag="qwen3:8b")
    monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", "http://127.0.0.1:9")

    with caplog.at_level(logging.INFO, logger="core.provider_routing"):
        ranked = _ranked_ids(registry)

    assert "ollama-local:qwen3:8b" not in ranked
    assert [r.getMessage() for r in caplog.records if "ollama-local:qwen3:8b" in r.getMessage()] == [
        "Hardware fit: ollama-local:qwen3:8b left out of this turn "
        "(model_exceeds_hardware_budget; needs 7.50 GB, this host allows 6.80 GB)."
    ]


def test_a_model_too_big_for_graphics_memory_and_ram_is_refused(home, monkeypatch) -> None:
    """Negative: an 8 GB card and 8 GB of RAM cannot hold qwen3:14b (8.6 GiB measured, ~10.1 GB)."""
    from core.agent_runtime.memory_runtime import chat_surface_honest_degraded_response

    _gpu_host(monkeypatch, ram_gb=8.0, vram_gb=STEVE_VRAM_GB)
    registry = ModelRegistry()
    register_installed_local_model(registry, model_tag="qwen3:14b")
    monkeypatch.setattr("core.local_model_presence.local_model_running", lambda **_: True)
    with _FakeOllama([{"name": "qwen3:14b", "size": QWEN3_14B_BYTES}]) as fake:
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        ranked = _ranked_ids(registry)
        shortfall = local_hardware_shortfall()
        text = chat_surface_honest_degraded_response(None, _Execution())

    assert "ollama-local:qwen3:14b" not in ranked
    assert shortfall == {
        "model": "qwen3:14b",
        "required_gb": pytest.approx(10.14, abs=0.01),
        "available_gb": pytest.approx(6.8, abs=0.01),
        "memory": "graphics",
    }
    assert text == (
        "I couldn't get a live model response in this run: `qwen3:14b` needs about 10.1 GB of graphics "
        "memory and this computer's graphics card can spare about 6.8 GB for it, so I didn't start it. "
        "Add a cloud key in Settings → API Keys, or run VOOL on a graphics card with more memory."
    )


def test_a_graphics_refusal_with_a_cloud_model_also_names_a_smaller_local_model(home, monkeypatch) -> None:
    """When the refusal is right, a cloud model is not offered as the only way to get an answer."""
    from core.agent_runtime.memory_runtime import chat_surface_honest_degraded_response
    from storage.model_provider_manifest import ModelProviderManifest, upsert_provider_manifest

    _gpu_host(monkeypatch, ram_gb=8.0, vram_gb=STEVE_VRAM_GB)
    registry = ModelRegistry()
    register_installed_local_model(registry, model_tag="qwen3:14b")
    upsert_provider_manifest(
        ModelProviderManifest(
            provider_name="openai-compatible-remote",
            model_name="cloud-test-model",
            source_type="http",
            capabilities=["summarize"],
            runtime_config={"base_url": "https://api.example.com/v1"},
        )
    )
    monkeypatch.setattr("core.local_model_presence.local_model_running", lambda **_: True)
    with _FakeOllama([{"name": "qwen3:14b", "size": QWEN3_14B_BYTES}]) as fake:
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        _ranked_ids(registry)
        text = chat_surface_honest_degraded_response(None, _Execution())

    assert text == (
        "I couldn't get a live model response in this run: `qwen3:14b` needs about 10.1 GB of graphics "
        "memory and this computer's graphics card can spare about 6.8 GB for it, so I didn't start it. "
        "Download a smaller local model in Setup under “Where should it think?”, or pick a cloud model "
        "in the model selector."
    )
