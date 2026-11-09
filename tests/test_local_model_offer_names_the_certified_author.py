"""Settings -> Local models offers the bundle's certified local author when the machine can run it.

Capability tests A7/D1/D2 (0.7.1-beta, clean Windows 11 VM, 16 GB RAM, no GPU): the Download buttons
offered qwen3:0.6b, qwen3:4b and gemma3:4b only. qwen3:8b -- the model the installed bundle names
(``bundle_manifest.json`` ``selected_model``, the certified local author per build_bundle.ps1), the
model the chat refusal names, and the model docs/getting-started/install.md says runs with at least
10 GB of RAM on a PC without a graphics card -- was never offered. The offer came from the hardware
bucket alone: a 16 GB VM reports a little under 16 GiB (bucket A), and no CPU bucket below C offers
qwen3:8b at all. The plan now puts the bundle's model first when the machine meets its documented RAM
floor; a model without a documented floor, or a machine below it, keeps the bucket offer unchanged.

After one model was installed the other Download buttons disappeared: Settings mounted the download
box only while no model was installed.
"""
from __future__ import annotations

import json
import re

import pytest

from core import hardware_tier, install_recommendations, local_model_pull
from core.hardware_tier import MachineProbe


def _probe(ram_gb: float, *, accelerator: str = "cpu", vram_gb: float | None = None) -> MachineProbe:
    return MachineProbe(cpu_cores=4, ram_gb=ram_gb, gpu_name=None, vram_gb=vram_gb, accelerator=accelerator)


@pytest.fixture
def machine(monkeypatch, tmp_path):
    def setup(*, ram_gb: float, selected_model: str | None, free_gb: float = 60.0) -> None:
        probe = _probe(ram_gb)
        monkeypatch.setattr(hardware_tier, "probe_machine", lambda: probe)
        monkeypatch.setattr(install_recommendations, "probe_machine", lambda: probe)
        monkeypatch.setattr(install_recommendations, "_free_gb", lambda runtime_home: free_gb)
        monkeypatch.setattr(install_recommendations, "_maybe_probe_gpu_capability", lambda *a, **k: None)
        for name in ("VOOL_BUNDLE_MANIFEST", "VOOL_BUNDLE_ROOT", "VOOL_ROOT", "VOOL_OLLAMA_MODEL", "VOOL_FORCE_OLLAMA_MODEL"):
            monkeypatch.delenv(name, raising=False)
        if selected_model is not None:
            manifest = tmp_path / "bundle_manifest.json"
            manifest.write_text(json.dumps({"schema": "vool.bundle_manifest.v1", "selected_model": selected_model}),
                                encoding="utf-8")
            monkeypatch.setenv("VOOL_BUNDLE_MANIFEST", str(manifest))
    return setup


@pytest.mark.parametrize("ram_gb", [15.7, 16.0, 12.0, 10.0])
def test_a_cpu_only_pc_with_ten_gb_or_more_is_offered_the_bundle_author_first(machine, ram_gb) -> None:
    machine(ram_gb=ram_gb, selected_model="qwen3:8b")
    names, _free = local_model_pull._plan()
    assert names[0] == "qwen3:8b", names


def test_the_bucket_models_are_still_offered_after_it(machine) -> None:
    machine(ram_gb=15.7, selected_model="qwen3:8b")
    names, _free = local_model_pull._plan()
    assert names == ("qwen3:8b", "qwen3:0.6b", "qwen3:4b", "gemma3:4b")


def test_below_the_documented_floor_the_bundle_author_is_not_offered(machine) -> None:
    machine(ram_gb=8.0, selected_model="qwen3:8b")
    names, _free = local_model_pull._plan()
    assert "qwen3:8b" not in names


def test_a_bundle_model_without_a_documented_floor_is_not_added(machine) -> None:
    machine(ram_gb=15.7, selected_model="qwen2.5:7b")
    names, _free = local_model_pull._plan()
    assert names == ("qwen3:0.6b", "qwen3:4b", "gemma3:4b")


def test_without_a_bundle_manifest_the_offer_is_the_bucket_plan(machine) -> None:
    machine(ram_gb=15.7, selected_model=None)
    names, _free = local_model_pull._plan()
    assert names == ("qwen3:0.6b", "qwen3:4b", "gemma3:4b")


def test_the_offered_author_can_be_started_by_a_click(machine, monkeypatch) -> None:
    """The click door matches the extended plan exactly: qwen3:8b is no longer model_not_offered."""
    machine(ram_gb=15.7, selected_model="qwen3:8b")
    monkeypatch.setattr(local_model_pull.policy_engine, "local_only_mode", lambda: True)
    status, payload = local_model_pull.start("qwen3:8b")
    # local-only mode still wins over the click; the point is that the name passed the offer check.
    assert (status, payload["error"]) == (409, "offline_mode")


def _widget_local_models_source() -> str:
    from core.vool_settings_page import render_vool_settings_html

    page = render_vool_settings_html(build_commit="test-commit")
    start = page.index("function widgetLocalModels(")
    end = page.index("\nfunction ", start + 1)
    return page[start:end]


def test_settings_keeps_the_download_box_once_a_model_is_installed() -> None:
    source = _widget_local_models_source()
    loop = source.index("for (const m of models)")
    after_list = source[loop:]
    assert re.search(r"VoolModelDownload\.mount\(", after_list), (
        "the download box must also be mounted under the installed-model list, so the other offered "
        "models stay downloadable after one is installed"
    )


def test_settings_still_offers_the_download_when_nothing_is_installed() -> None:
    source = _widget_local_models_source()
    empty = source.index("if (!models.length)")
    loop = source.index("for (const m of models)")
    assert "VoolModelDownload.mount(" in source[empty:loop]
