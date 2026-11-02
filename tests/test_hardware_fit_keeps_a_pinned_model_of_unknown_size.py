"""A model the user pinned is not dropped by the hardware-fit gate on a GUESSED size alone.

A model served from this machine's own OpenAI-compatible endpoint (LM Studio, llama.cpp, a
custom BYOK lane on 127.0.0.1) can carry a name with no size in it and no measured artifact.
Its footprint is then the 8B fallback guess (7.5 GB resident). On an 8 GB graphics card that
guess alone dropped the user's PINNED, certified lane from the turn: it ended with "its provider
lane is not available right now" although the endpoint was up (Windows, RTX 4060,
tests/test_x_editorial_served.py).

The exception is scoped to the explicit pin: the user chose that exact model, and if it truly
does not fit, the failure surfaces on that turn as that provider's error, never as a silent
substitute. Everything else still fails closed: an UNPINNED model of unknown size is still
dropped, and a pinned model whose size IS known (in its name, its metadata, a measured artifact
or a stated budget) is still dropped when it does not fit.

LABELLED: virtual hosts (patched `_device_probe`), real manifests and the real ranking gate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import provider_routing
from storage.model_provider_manifest import ModelProviderManifest

LOOPBACK = "http://127.0.0.1:1234/v1"


@pytest.fixture
def eight_gb_graphics_card(monkeypatch):
    """The Windows host that showed it: 8 GB VRAM, so the gate allows 6.8 GB."""
    probe = SimpleNamespace(ram_gb=32.0, vram_gb=8.0, accelerator="cuda")
    monkeypatch.setattr(provider_routing, "_device_probe", lambda: probe)


def _loopback_manifest(model_name: str, **metadata) -> ModelProviderManifest:
    return ModelProviderManifest(
        provider_name="custom-byok",
        model_name=model_name,
        source_type="http",
        capabilities=["summarize"],
        runtime_config={"base_url": LOOPBACK},
        metadata=dict(metadata),
    )


def _ranked(manifest: ModelProviderManifest, *, pinned: bool) -> list[str]:
    registry = SimpleNamespace(rank_manifests=lambda request: [manifest])
    ranked = provider_routing.rank_provider_candidates(
        registry, task_kind="chat", output_mode="text",
        preferred_provider="custom-byok" if pinned else None,
        preferred_model=manifest.model_name if pinned else None,
        allow_paid_fallback=True, swarm_size=4, enforce_hardware_fit=True)
    return [m.provider_id for m in ranked]


def test_the_pinned_model_of_unknown_size_stays_in_the_pool(eight_gb_graphics_card) -> None:
    manifest = _loopback_manifest("probe-model")
    assert _ranked(manifest, pinned=True) == [manifest.provider_id]


def test_the_gate_still_reports_the_guess_does_not_fit(eight_gb_graphics_card) -> None:
    truth = provider_routing.provider_capability_truth_for_manifest(_loopback_manifest("probe-model"))
    assert truth.locality == "local"
    assert truth.hardware_fit_reason == "model_exceeds_hardware_budget", truth


def test_an_unpinned_model_of_unknown_size_is_still_dropped(eight_gb_graphics_card) -> None:
    assert _ranked(_loopback_manifest("probe-model"), pinned=False) == []


@pytest.mark.parametrize(
    ("model_name", "metadata"),
    [
        ("qwen2.5:14b", {}),
        ("probe-model", {"artifact_size_gb": 9.0}),
        ("probe-model", {"vram_budget_gb": 9.0}),
    ],
    ids=["size-in-the-name", "measured-artifact", "declared-budget"],
)
def test_a_pinned_model_whose_known_size_does_not_fit_is_still_dropped(
    eight_gb_graphics_card, model_name, metadata
) -> None:
    assert _ranked(_loopback_manifest(model_name, **metadata), pinned=True) == []
