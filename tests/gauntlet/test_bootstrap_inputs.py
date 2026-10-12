"""The scripted machine must supply setup facts, not override setup decisions."""
from __future__ import annotations

import subprocess
import sys

import pytest

from core import local_ollama_inventory, system_resources
from core.local_model_selection import select_local_model_for_provisioning
from core.web.api import runtime as api_runtime
from tests.gauntlet import harness as H  # noqa: N812


def _no_host_probe(*args, **kwargs):
    raise AssertionError("scripted machine must not read live host resources")


@pytest.mark.parametrize("ram_gb", [2.0, 32.0])
def test_resource_snapshot_matches_the_scripted_machine(monkeypatch, ram_gb):
    monkeypatch.setattr(system_resources, "_build_snapshot", _no_host_probe)
    H.install(monkeypatch, machine=H.machine_probe(ram_gb=ram_gb))
    snapshot = system_resources.snapshot(force=True)
    assert snapshot.total_ram_gb == ram_gb
    assert snapshot.available_ram_gb == pytest.approx(ram_gb * 0.8)
    assert snapshot.available_ram_gb + snapshot.used_ram_gb == ram_gb
    assert snapshot.reserve_gb == pytest.approx(ram_gb * 0.2)
    assert snapshot.ollama_loaded_gb == 0.0


def test_inventory_is_exact_and_does_not_claim_residency(monkeypatch):
    H.install(monkeypatch)
    installed = local_ollama_inventory.installed_ollama_model_names(env={})
    assert "qwen3:14b" in installed
    assert "qwen3:14b-instruct" not in installed
    assert "qwen2.5:72b" not in installed
    assert local_ollama_inventory.loaded_ollama_model_names(env={}) == ()


@pytest.mark.parametrize("ram_gb,accepted", [(2.0, False), (32.0, True)])
def test_real_cold_load_gate_still_accepts_and_refuses(monkeypatch, ram_gb, accepted):
    H.install(monkeypatch, machine=H.machine_probe(ram_gb=ram_gb))
    result = select_local_model_for_provisioning(
        selection_mode="explicit",
        requested_model="qwen3:14b",
        probe=H.machine_probe(ram_gb=ram_gb),
        free_disk_gb=api_runtime.free_disk_gb_for_path("unused-fixture-model-store"),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )
    assert result.selected is accepted
    if not accepted:
        assert result.result == "model_load_gated_low_memory"


def test_disk_read_is_a_fixed_fixture_input(monkeypatch):
    H.install(monkeypatch)
    assert api_runtime.free_disk_gb_for_path("unused-fixture-model-store") == 128.0


def test_managed_install_restores_all_fixture_seams():
    from adapters.openai_compatible_adapter import OpenAICompatibleAdapter

    seams = (
        (system_resources, "snapshot"),
        (api_runtime, "free_disk_gb_for_path"),
        (local_ollama_inventory, "_ollama_models_payload"),
        (OpenAICompatibleAdapter, "tool_certification_runtime_identity"),
    )
    originals = [getattr(owner, name) for owner, name in seams]
    with pytest.MonkeyPatch.context() as patch:
        H.install(patch)
        assert all(getattr(owner, name) is not original for (owner, name), original in zip(seams, originals, strict=True))
    assert all(getattr(owner, name) is original for (owner, name), original in zip(seams, originals, strict=True))


def test_child_refuses_network_without_mutating_the_parent(tmp_path):
    from tests import _network_seal
    from tests.gauntlet.hermetic.runner import child_env

    before = _network_seal.snapshot_stack()
    # The underlying transports are tripwires, so even the expected-red run sends no packets.
    script = '''
import socket
def escaped(*args, **kwargs):
    raise AssertionError("network escaped the hermetic child")
socket.socket.connect = escaped
socket.socket.connect_ex = escaped
socket.create_connection = escaped
from tests.gauntlet.hermetic.child import main
assert main(["child", "no_such_group"]) == 3
with socket.socket() as client:
    for connect in (
        lambda: socket.create_connection(("198.51.100.1", 9), timeout=0.1),
        lambda: client.connect(("198.51.100.1", 9)),
        lambda: client.connect_ex(("198.51.100.1", 9)),
    ):
        try:
            connect()
        except OSError as exc:
            assert "hermetic gauntlet" in str(exc)
        else:
            raise AssertionError("network was allowed")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=child_env(tmp_path, port=49300),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert _network_seal.snapshot_stack() == before
