from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from core.local_model_selection import (
    AUTO_SELECTION_MODE,
    EXPLICIT_SELECTION_MODE,
    resolve_model_selection_mode,
    select_local_model_for_provisioning,
)
from core.resource_governor import cold_model_load_admission


def _snapshot(available: float) -> SimpleNamespace:
    return SimpleNamespace(available_ram_gb=available)


def _probe() -> dict[str, object]:
    return {"ram_gb": 10.0, "accelerator": "cpu", "vram_gb": 0.0}


def test_auto_rejects_7b_before_selecting_smaller_daily_model() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(8.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )

    assert selection.selected_model == "qwen3:4b"
    assert [item.result for item in selection.candidates[:2]] == [
        "model_load_gated_low_memory",
        "selected",
    ]
    assert selection.candidates[0].model == "qwen2.5:7b"
    assert selection.candidates[1].role == "daily_accelerated"


def test_auto_keeps_7b_when_cold_load_fits() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(10.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )

    assert selection.selected_model == "qwen2.5:7b"
    assert len(selection.candidates) == 1
    assert selection.candidates[0].result == "selected"


def test_auto_does_not_substitute_an_already_installed_model_on_a_later_boot() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(8.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
        already_installed=True,
    )

    assert selection.selected_model == "qwen2.5:7b"
    assert selection.result == "selected"
    assert "ordinary runtime admission" in selection.reason


def test_explicit_7b_refuses_without_substitution_when_memory_is_low() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=EXPLICIT_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(8.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )

    assert selection.selected_model == ""
    assert selection.result == "model_load_gated_low_memory"
    assert len(selection.candidates) == 1
    assert "No substitute" in selection.reason


def test_explicit_model_remains_selected_when_memory_becomes_available() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=EXPLICIT_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(10.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )

    assert selection.selected_model == "qwen2.5:7b"
    assert selection.candidates[0].result == "selected"


def test_local_only_never_falls_back_to_cloud_when_local_policy_is_disabled() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(10.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "0"},
    )

    assert selection.result == "policy_disabled"
    assert selection.selected_model == ""
    assert not selection.policy_enabled


def test_auto_does_not_invoke_inventory_or_download_during_candidate_selection() -> None:
    with mock.patch("core.local_ollama_inventory.installed_ollama_model_inventory") as inventory:
        selection = select_local_model_for_provisioning(
            selection_mode=AUTO_SELECTION_MODE,
            requested_model="qwen2.5:7b",
            probe=_probe(),
            free_disk_gb=100.0,
            snapshot=_snapshot(8.0),
            env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
        )

    inventory.assert_not_called()
    assert selection.selected_model == "qwen3:4b"
    assert all(item.result != "selected" or item.model == "qwen3:4b" for item in selection.candidates)


def test_recommended_gpu_backend_is_typed_unsupported_without_that_backend() -> None:
    recommendation = SimpleNamespace(
        recommended_bundle=SimpleNamespace(
            role_models=(
                SimpleNamespace(role="daily_accelerated", model="gpu:7b", backend="llamacpp"),
                SimpleNamespace(role="tiny_fast", model="tiny:0.6b", backend="ollama"),
            )
        ),
        fallback_bundle=SimpleNamespace(role_models=()),
    )
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(10.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
        recommendation=recommendation,
    )

    assert selection.candidates[0].result == "backend_unsupported"
    assert selection.selected_model == "tiny:0.6b"


def test_nonresident_fit_stays_a_cold_load_with_load_required() -> None:
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen3:4b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=_snapshot(8.0),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )
    candidate = selection.candidates[0]

    assert candidate.result == "selected"
    assert candidate.resident is False
    assert candidate.load_required is True


def test_selector_and_execution_gate_use_the_same_cold_load_formula() -> None:
    snapshot = _snapshot(8.0)
    selection = select_local_model_for_provisioning(
        selection_mode=AUTO_SELECTION_MODE,
        requested_model="qwen2.5:7b",
        probe=_probe(),
        free_disk_gb=100.0,
        snapshot=snapshot,
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
    )

    direct = cold_model_load_admission("qwen2.5:7b", snapshot=snapshot)
    assert direct.ok is False
    assert selection.candidates[0].result == "model_load_gated_low_memory"
    assert selection.candidates[0].required_headroom_gb == direct.footprint_gb + 1.5


def test_bundle_recommendation_and_persisted_default_do_not_create_an_explicit_pin() -> None:
    assert resolve_model_selection_mode(
        provider_env={"VOOL_OLLAMA_MODEL": "qwen2.5:7b", "VOOL_BUNDLE_MODEL": "qwen2.5:7b"},
        process_env={"VOOL_OLLAMA_MODEL": "qwen2.5:7b", "VOOL_BUNDLE_MODEL": "qwen2.5:7b"},
        persisted_model="",
    ) == AUTO_SELECTION_MODE
    assert resolve_model_selection_mode(
        provider_env={"VOOL_OLLAMA_MODEL": "qwen2.5:7b"},
        process_env={"VOOL_OLLAMA_MODEL": "qwen2.5:7b"},
        persisted_model="qwen2.5:7b",
    ) == AUTO_SELECTION_MODE


def test_direct_process_override_is_explicit() -> None:
    assert resolve_model_selection_mode(
        provider_env={},
        process_env={"VOOL_OLLAMA_MODEL": "qwen2.5:7b"},
        persisted_model="qwen3:4b",
    ) == EXPLICIT_SELECTION_MODE
