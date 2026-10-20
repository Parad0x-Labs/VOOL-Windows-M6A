"""On a PC with an NVIDIA (CUDA) or DirectML graphics card, the memory refusal names graphics memory.

The hardware-fit gate measures a local model against the graphics card's memory there (VRAM minus
1.2 GB), not the computer's RAM. The refusal used to say "this computer can spare about 4.8 GB" on a
32 GB PC with a 6 GB card, and told the user to find a computer with more memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import provider_routing
from core.provider_routing import local_hardware_shortfall
from tests.test_local_model_memory_fit import _registered_qwen3_8b, home


class _Execution:
    source = "no_provider_available"
    details = {}


def _gpu_host(monkeypatch, *, accelerator: str, ram_gb: float, vram_gb: float) -> None:
    probe = SimpleNamespace(ram_gb=ram_gb, vram_gb=vram_gb, accelerator=accelerator)
    monkeypatch.setattr(provider_routing, "_device_probe", lambda: probe)


@pytest.mark.parametrize("accelerator", ["cuda", "directml"])
def test_a_gpu_host_is_told_the_graphics_card_is_too_small(home, monkeypatch, accelerator) -> None:
    from core.agent_runtime.memory_runtime import chat_surface_honest_degraded_response

    _gpu_host(monkeypatch, accelerator=accelerator, ram_gb=32.0, vram_gb=6.0)
    _registered_qwen3_8b(monkeypatch, measured=True)
    monkeypatch.setattr("core.local_model_presence.local_model_running", lambda **_: True)

    assert local_hardware_shortfall() == {
        "model": "qwen3:8b",
        "required_gb": pytest.approx(6.37),
        "available_gb": pytest.approx(4.8),
        "memory": "graphics",
    }
    assert chat_surface_honest_degraded_response(None, _Execution()) == (
        "I couldn't get a live model response in this run: `qwen3:8b` needs about 6.4 GB of graphics memory "
        "and this computer's graphics card can spare about 4.8 GB for it, so I didn't start it. Add a cloud "
        "key in Settings → API Keys, or run VOOL on a graphics card with more memory."
    )


def test_a_gpu_host_forced_to_cpu_is_measured_against_its_ram(home, monkeypatch) -> None:
    # VOOL_OLLAMA_NUM_GPU=0 keeps every layer on the CPU, so the gate measures RAM, not the card.
    monkeypatch.setenv("VOOL_OLLAMA_NUM_GPU", "0")
    _gpu_host(monkeypatch, accelerator="cuda", ram_gb=8.0, vram_gb=6.0)
    _registered_qwen3_8b(monkeypatch, measured=True)

    shortfall = local_hardware_shortfall()

    assert shortfall["available_gb"] == pytest.approx(5.6)
    assert "memory" not in shortfall
