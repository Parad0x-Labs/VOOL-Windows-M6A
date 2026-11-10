"""On a discrete graphics card, the local model's context window is sized so the model stays on the GPU.

Measured by Steve on an RTX 4060 8 GB (8188 MiB) with qwen3:8b Q4_K_M (4.87 GiB), 32 GB RAM:

    num_ctx 24576: 9.1 GB, 31%/69% CPU/GPU, 18.8 tok/s   <- what VOOL picked (bucket E, general role)
    num_ctx 12288: 7.2 GB, 13%/87% CPU/GPU, 19.3 tok/s
    num_ctx  8192: 6.2 GB, 100% GPU,        38.3 tok/s
    num_ctx  4096: 5.6 GB, 100% GPU,        40.3 tok/s

The KV cache was 3456 MiB at 24576 tokens: 0.1406 MiB per token, which is qwen3:8b's geometry
(36 layers x 8 KV heads x 128 head dim x 2 (K and V) x 2 bytes at f16). The RAM-sized bucket
chose 24576 and pushed a third of the model onto the CPU at half the speed.

Now, on a CUDA or DirectML card, the adaptive sizing picks the largest window at which the model's
weights, its KV cache and a fixed runtime overhead fit the card's spare memory (VRAM minus the same
1.2 GB the hardware-fit gate keeps), never below 8192, and never above what the bucket chose. A
16 GB card keeps 24576; VOOL_OLLAMA_CONTEXT_WINDOW still wins; a CPU host and a forced CPU-only
(VOOL_OLLAMA_NUM_GPU=0) host are unchanged.

LABELLED: virtual hosts (patched ``core.hardware_tier.probe_machine``), the real sizing function
and the real manifest factory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.hardware_tier as hardware_tier
import core.runtime_provider_defaults as provider_defaults
from core.runtime_provider_defaults import _ollama_context_sizing

QWEN3_8B_GIB = 5_225_388_164 / 1024.0**3  # 4.87, Ollama's /api/tags size
RTX_4060_VRAM_GB = 8188 / 1024.0


def _host(monkeypatch, *, vram_gb: float, ram_gb: float = 32.0, accelerator: str = "cuda") -> None:
    probe = SimpleNamespace(ram_gb=ram_gb, vram_gb=vram_gb, accelerator=accelerator, cpu_cores=16)
    monkeypatch.setattr(hardware_tier, "probe_machine", lambda: probe)


def _num_ctx(*, artifact_size_gb: float = QWEN3_8B_GIB, env: dict | None = None) -> int:
    return int(
        _ollama_context_sizing(
            "general", model_tag="qwen3:8b", bucket="E", env=env or {}, artifact_size_gb=artifact_size_gb
        )["selected_num_ctx"]
    )


def test_qwen3_8b_kv_per_token_matches_the_measured_cache() -> None:
    assert provider_defaults._kv_cache_mib_per_token("qwen3:8b") == pytest.approx(3456 / 24576, rel=0.01)
    # A q8_0 KV cache holds half the bytes per token.
    assert provider_defaults._kv_cache_mib_per_token("qwen3:8b", kv_quant="q8_0") == pytest.approx(
        3456 / 24576 / 2, rel=0.01
    )


def test_a_model_with_no_known_geometry_is_sized_from_its_parameter_count() -> None:
    per_token = provider_defaults._kv_cache_mib_per_token("somefamily:8b")
    assert per_token == pytest.approx(0.14, abs=0.01)


@pytest.mark.parametrize(
    ("num_ctx", "fits"),
    [(24576, False), (12288, False), (8192, True), (4096, True)],
)
def test_the_footprint_agrees_with_steves_table(num_ctx, fits) -> None:
    """24576 and 12288 spilled onto the CPU on the 8 GB card; 8192 and 4096 ran 100% on the GPU."""
    need = provider_defaults._gpu_resident_gb("qwen3:8b", num_ctx=num_ctx, artifact_size_gb=QWEN3_8B_GIB)
    assert (need <= RTX_4060_VRAM_GB - 1.2) is fits


@pytest.mark.parametrize("accelerator", ["cuda", "directml"])
def test_an_8gb_card_gets_8192(monkeypatch, accelerator) -> None:
    _host(monkeypatch, vram_gb=RTX_4060_VRAM_GB, accelerator=accelerator)
    sizing = _ollama_context_sizing(
        "general", model_tag="qwen3:8b", bucket="E", env={}, artifact_size_gb=QWEN3_8B_GIB
    )
    assert sizing["selected_num_ctx"] == 8192
    assert sizing["context_sizing_policy"] == "adaptive_fits_graphics_memory"
    assert sizing["graphics_memory_spare_gb"] == pytest.approx(6.8, abs=0.01)


def test_an_8gb_card_gets_8192_before_ollama_measured_the_model(monkeypatch) -> None:
    # Without the artifact size, the weights are the parameter estimate (8 x 0.75 = 6 GB): the
    # window still never drops below 8192.
    _host(monkeypatch, vram_gb=RTX_4060_VRAM_GB)
    assert _num_ctx(artifact_size_gb=0.0) == 8192


def test_a_6gb_card_is_never_sized_below_8192(monkeypatch) -> None:
    _host(monkeypatch, vram_gb=6.0)
    assert _num_ctx() == 8192


def test_a_16gb_card_keeps_24576(monkeypatch) -> None:
    _host(monkeypatch, vram_gb=16.0)
    assert _num_ctx() == 24576


def test_the_context_override_still_wins_on_an_8gb_card(monkeypatch) -> None:
    _host(monkeypatch, vram_gb=RTX_4060_VRAM_GB)
    assert _num_ctx(env={"VOOL_OLLAMA_CONTEXT_WINDOW": "24576"}) == 24576
    assert _num_ctx(env={"VOOL_OLLAMA_CONTEXT_WINDOW": "4096"}) == 4096


def test_a_cpu_host_is_unchanged(monkeypatch) -> None:
    _host(monkeypatch, vram_gb=0.0, accelerator="cpu")
    sizing = _ollama_context_sizing("general", model_tag="qwen3:8b", bucket="E", env={}, artifact_size_gb=QWEN3_8B_GIB)
    assert sizing["selected_num_ctx"] == 24576
    assert "graphics_memory_spare_gb" not in sizing


def test_a_host_forced_to_cpu_is_unchanged(monkeypatch) -> None:
    _host(monkeypatch, vram_gb=RTX_4060_VRAM_GB)
    assert _num_ctx(env={"VOOL_OLLAMA_NUM_GPU": "0"}) == 24576


def test_the_registered_lane_carries_the_gpu_sized_window(monkeypatch) -> None:
    from storage.model_provider_manifest import ModelProviderManifest

    _host(monkeypatch, vram_gb=RTX_4060_VRAM_GB)
    manifests: dict[tuple[str, str], ModelProviderManifest] = {}
    registry = SimpleNamespace(
        get_manifest=lambda provider, model: manifests.get((provider, model)),
        register_manifest=lambda manifest: manifests.__setitem__((manifest.provider_name, manifest.model_name), manifest),
    )
    env = {"VOOL_CONTEXT_BUCKET": "E", "VOOL_RAW_OLLAMA_API_URL": "http://127.0.0.1:9"}

    assert provider_defaults._ensure_local_ollama_provider(
        registry, model_tag="qwen3:8b", bundle_role="general", bucket="E", env=env
    )
    manifest = manifests[("ollama-local", "qwen3:8b")]
    assert manifest.runtime_config["context_window"] == 8192
    assert manifest.runtime_config["prewarm"]["options"]["num_ctx"] == 8192
