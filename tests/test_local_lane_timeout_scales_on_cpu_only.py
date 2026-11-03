"""The local Ollama lane's answer timeout and the CPU fallback budget scale together on a CPU.

Mac Intel smoke "recall answers from memory" (runs 41531657501, 41568073330, 41597602096,
i7-8700B, qwen3:8b): the recall answer ran 198-207 s against a flat 180 s timeout and the turn
failed. Both 180 s ceilings have to move, or the router's budget caps the call back to 180 s.
"""

from __future__ import annotations

import pytest

from core import local_compute_budget
from core.hardware_tier import MachineProbe
from core.memory_first_router import resolve_fallback_budget_seconds
from core.runtime_provider_defaults import _ollama_answer_timeout_seconds


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    local_compute_budget.local_inference_is_cpu_only.cache_clear()
    yield
    local_compute_budget.local_inference_is_cpu_only.cache_clear()


def _machine(monkeypatch, accelerator: str) -> None:
    def probe() -> MachineProbe:
        return MachineProbe(cpu_cores=6, ram_gb=32.0, gpu_name=None, vram_gb=None, accelerator=accelerator)

    monkeypatch.setattr("core.hardware_tier.probe_machine", probe)


@pytest.mark.parametrize("accelerator", ["mps", "cuda"])
def test_a_gpu_machine_keeps_the_measured_180_seconds(monkeypatch, accelerator):
    _machine(monkeypatch, accelerator)
    assert _ollama_answer_timeout_seconds(None) == 180


def test_a_cpu_only_machine_gets_the_scaled_timeout(monkeypatch):
    _machine(monkeypatch, "cpu")
    scaled = int(180 * local_compute_budget.CPU_ONLY_FACTOR)
    assert _ollama_answer_timeout_seconds(None) == scaled
    # The slowest failed smoke recall (207 s) fits with room for a slower CPU.
    assert scaled > 207


def test_forcing_the_model_onto_the_cpu_scales_it_on_a_gpu_machine_too(monkeypatch):
    _machine(monkeypatch, "cuda")
    assert _ollama_answer_timeout_seconds(0) == int(180 * local_compute_budget.CPU_ONLY_FACTOR)
    # A forced partial offload still runs on the GPU.
    assert _ollama_answer_timeout_seconds(20) == 180


def test_the_cpu_fallback_budget_never_caps_the_scaled_timeout_back(monkeypatch):
    _machine(monkeypatch, "cpu")
    budget = resolve_fallback_budget_seconds("daily", forced_cpu=False, no_usable_gpu=True)
    assert budget is not None and budget >= _ollama_answer_timeout_seconds(None)
    assert resolve_fallback_budget_seconds("daily", forced_cpu=True, no_usable_gpu=False) >= _ollama_answer_timeout_seconds(0)
