"""A local model that is too big for this computer: one size for boot and every turn, said plainly.

Measured on a clean Windows 11 VM (CPU only, 10 GB RAM) at f8d2e30: boot sized qwen3:8b from
Ollama's /api/tags bytes and judged it to fit, but every turn's hardware-fit gate used the
parameter-count estimate (8 x 0.75 + 1.5 = 7.5 GB) against 0.70 x RAM (about 7.0 GB), dropped the
model without a log line and answered "I couldn't get a live model response in this run" with
no reason. The same model loaded on an 11 GB VM used about 6.3 GB.

Now the registered lane carries Ollama's measured size, so the turn gate reads the same number as
boot. When no local model fits, the routing log and the refusal both say how much memory the model
needs and how much this computer can give it.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import local_model_presence, local_ollama_inventory, provider_routing
from core.local_ollama_inventory import installed_ollama_model_inventory
from core.model_registry import ModelRegistry
from core.provider_routing import (
    estimated_local_model_resident_gb,
    local_hardware_shortfall,
    provider_capability_truth_for_manifest,
    rank_provider_candidates,
)
from core.runtime_provider_defaults import record_ollama_artifact_sizes, register_installed_local_model

# conftest stubs the /api/tags read for every test (no live Ollama); these tests read a loopback
# fake instead, through the real reader captured here at import, before that stub is applied.
_REAL_OLLAMA_MODELS_PAYLOAD = local_ollama_inventory._ollama_models_payload

# Ollama's /api/tags size for qwen3:8b (5.2 GB) and the RAM Windows reported on the 10 GB VM.
QWEN3_8B_BYTES = 5_225_388_164
VM_10GB_RAM_GB = 9.98


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "memory-fit-home"
    (home / "data").mkdir(parents=True)
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("VOOL_LOCAL_MODELS_ENABLED", raising=False)
    monkeypatch.delenv("VOOL_INSTALLED_OLLAMA_MODELS", raising=False)
    monkeypatch.delenv("VOOL_OLLAMA_NUM_GPU", raising=False)
    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload", _REAL_OLLAMA_MODELS_PAYLOAD)
    from core.runtime_paths import configure_runtime_home

    configure_runtime_home(home)
    import storage.db as sdb

    sdb.configure_default_db_path(home / "data" / "vool_web0_v2.db")
    local_model_presence.reset_cache()
    yield home
    local_model_presence.reset_cache()
    configure_runtime_home(None)
    sdb.configure_default_db_path(None)


def _cpu_host(monkeypatch, ram_gb: float) -> None:
    probe = SimpleNamespace(ram_gb=ram_gb, vram_gb=0.0, accelerator="cpu")
    monkeypatch.setattr(provider_routing, "_device_probe", lambda: probe)


class _FakeOllama:
    """A loopback Ollama answering /api/tags with the given rows."""

    def __init__(self, models: list[dict]):
        rows = models

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                found = self.path == "/api/tags"
                body = json.dumps({"models": rows}).encode() if found else b"{}"
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _registered_qwen3_8b(monkeypatch, *, measured: bool) -> ModelRegistry:
    registry = ModelRegistry()
    assert register_installed_local_model(registry, model_tag="qwen3:8b") is not None
    if measured:
        with _FakeOllama([{"name": "qwen3:8b", "size": QWEN3_8B_BYTES}]) as fake:
            monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
            inventory = installed_ollama_model_inventory(base_url=fake.url)
        assert record_ollama_artifact_sizes(registry, inventory) == ("ollama-local:qwen3:8b",)
    return registry


def test_the_turn_gate_sizes_an_installed_model_as_boot_does(home, monkeypatch) -> None:
    _cpu_host(monkeypatch, VM_10GB_RAM_GB)
    registry = _registered_qwen3_8b(monkeypatch, measured=True)

    capability = provider_capability_truth_for_manifest(registry.get_manifest("ollama-local", "qwen3:8b"))

    # Boot's residency pick sizes the same bytes with the same function (runtime.py /api/tags GiB).
    boot_estimate = estimated_local_model_resident_gb("qwen3:8b", artifact_size_gb=QWEN3_8B_BYTES / 1024.0**3)
    assert capability.ram_budget_gb == pytest.approx(boot_estimate, abs=0.01)
    assert capability.ram_budget_gb == pytest.approx(6.37, abs=0.01)
    # Measured resident use on the 11 GB VM was about 6.3 GB, so the estimate still covers it.
    assert capability.ram_budget_gb >= 6.31
    assert capability.hardware_fit is True and capability.hardware_fit_reason == ""
    candidates = rank_provider_candidates(
        registry, task_kind="summarize", output_mode="text", enforce_hardware_fit=True
    )
    assert "ollama-local:qwen3:8b" in [manifest.provider_id for manifest in candidates]


def test_without_a_measured_size_the_turn_gate_keeps_the_parameter_estimate(home, monkeypatch) -> None:
    _cpu_host(monkeypatch, VM_10GB_RAM_GB)
    registry = _registered_qwen3_8b(monkeypatch, measured=False)

    capability = provider_capability_truth_for_manifest(registry.get_manifest("ollama-local", "qwen3:8b"))

    assert capability.ram_budget_gb == pytest.approx(7.5)
    assert capability.hardware_fit_reason == "model_exceeds_hardware_budget"


def test_the_measured_size_survives_a_rewrite_of_the_lane(home, monkeypatch) -> None:
    registry = _registered_qwen3_8b(monkeypatch, measured=True)
    before = dict(registry.get_manifest("ollama-local", "qwen3:8b").metadata)
    # A new contract (here a forced CPU-only num_gpu) makes the factory rewrite the manifest.
    monkeypatch.setenv("VOOL_OLLAMA_NUM_GPU", "0")
    register_installed_local_model(registry, model_tag="qwen3:8b")
    after = registry.get_manifest("ollama-local", "qwen3:8b")

    assert after.runtime_config.get("num_gpu") == 0
    assert after.metadata["artifact_size_gb"] == before["artifact_size_gb"]


def test_recording_sizes_never_registers_a_model_that_is_not_already_a_lane(home) -> None:
    registry = ModelRegistry()
    inventory = (SimpleNamespace(name="qwen3:8b", size_bytes=QWEN3_8B_BYTES),)

    assert record_ollama_artifact_sizes(registry, inventory) == ()
    assert registry.get_manifest("ollama-local", "qwen3:8b") is None


def test_boot_records_the_installed_size_on_the_registered_lane(home, monkeypatch) -> None:
    from core.web.api.runtime import record_installed_ollama_artifact_sizes

    registry = ModelRegistry()
    register_installed_local_model(registry, model_tag="qwen3:8b")
    with _FakeOllama([{"name": "qwen3:8b", "size": QWEN3_8B_BYTES}]) as fake:
        record_installed_ollama_artifact_sizes(registry, env={"VOOL_RAW_OLLAMA_API_URL": fake.url})

    size = registry.get_manifest("ollama-local", "qwen3:8b").metadata["artifact_size_gb"]
    assert size == pytest.approx(QWEN3_8B_BYTES / 1024.0**3, abs=0.001)


def test_a_dropped_model_is_named_in_the_routing_log(home, monkeypatch, caplog) -> None:
    _cpu_host(monkeypatch, 8.0)
    registry = _registered_qwen3_8b(monkeypatch, measured=True)

    with caplog.at_level(logging.INFO, logger="core.provider_routing"):
        candidates = rank_provider_candidates(
            registry, task_kind="summarize", output_mode="text", enforce_hardware_fit=True
        )

    assert "ollama-local:qwen3:8b" not in [manifest.provider_id for manifest in candidates]
    lines = [record.getMessage() for record in caplog.records if "ollama-local:qwen3:8b" in record.getMessage()]
    assert lines == [
        "Hardware fit: ollama-local:qwen3:8b left out of this turn "
        "(model_exceeds_hardware_budget; needs 6.37 GB, this host allows 5.60 GB)."
    ]


def test_a_refused_turn_says_the_model_needs_more_memory(home, monkeypatch) -> None:
    from core.agent_runtime.memory_runtime import chat_surface_honest_degraded_response

    _cpu_host(monkeypatch, 8.0)
    _registered_qwen3_8b(monkeypatch, measured=True)
    # Ollama itself is up: the model is installed, it just doesn't fit this computer.
    monkeypatch.setattr("core.local_model_presence.local_model_running", lambda **_: True)

    class _Execution:
        source = "no_provider_available"
        details = {}

    text = chat_surface_honest_degraded_response(None, _Execution())

    assert text == (
        "I couldn't get a live model response in this run: `qwen3:8b` needs about 6.4 GB of memory "
        "and this computer can spare about 5.6 GB for it, so I didn't start it. Add a cloud key in "
        "Settings → API Keys, or run VOOL on a computer with more memory."
    )
    assert local_hardware_shortfall() == {
        "model": "qwen3:8b",
        "required_gb": pytest.approx(6.37),
        "available_gb": pytest.approx(5.6),
    }


def test_the_refusal_stays_silent_when_a_local_model_fits(home, monkeypatch) -> None:
    from core.agent_runtime.memory_runtime import chat_surface_honest_degraded_response

    _cpu_host(monkeypatch, 16.0)
    _registered_qwen3_8b(monkeypatch, measured=True)
    monkeypatch.setattr("core.local_model_presence.local_model_running", lambda **_: True)

    class _Execution:
        source = "no_provider_available"
        details = {}

    assert local_hardware_shortfall() == {}
    assert chat_surface_honest_degraded_response(None, _Execution()) == (
        "I couldn't get a live model response in this run, so I'm not going to recycle cached or "
        "remembered text as if it were fresh."
    )
