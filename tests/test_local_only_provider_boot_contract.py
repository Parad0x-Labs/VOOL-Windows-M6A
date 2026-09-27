from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.auto_local_only_mode import (
    CloudEgressBlockedError,
    assert_manifest_allowed,
    cloud_lane_permitted,
)
from core.runtime_bootstrap import (
    effective_runtime_provider_decision,
    resolve_backend_selection,
    runtime_provider_decision,
)
from core.runtime_capabilities import runtime_capability_statuses
from core.runtime_context import RuntimeFeatureFlags
from core.web.api.runtime import RuntimeServices, bind_runtime_provider_policy
from core.web.api.service import capability_snapshot_with_runtime
from storage.model_provider_manifest import ModelProviderManifest


class _NoBackend:
    def detect_hardware(self):
        return {"device": "cpu"}

    def select_backend(self, hardware):
        return SimpleNamespace(
            backend_name="NoBackend",
            device="none",
            reason="no_supported_backend",
        )

    def healthcheck(self, selection):
        return False


class _HealthyLocalBackend(_NoBackend):
    def select_backend(self, hardware):
        return SimpleNamespace(
            backend_name="TorchCPUBackend",
            device="cpu",
            reason="cpu_backend_ready",
        )

    def healthcheck(self, selection):
        return True


def test_local_only_with_usable_backend_selects_local() -> None:
    selection = resolve_backend_selection(
        manager=_HealthyLocalBackend(),
        local_only=True,
        allow_remote_only=False,
    )

    assert selection.backend_name == "TorchCPUBackend"
    decision = runtime_provider_decision(
        selection,
        local_only=True,
        allow_remote_only=False,
    )
    assert decision["effective_mode"] == "local_only"
    assert decision["selected_lane"] == "local"
    assert decision["local_backend_available"] is True
    assert decision["remote_request_eligible"] is False


def test_local_only_without_backend_is_unavailable_even_with_remote_override() -> None:
    selection = resolve_backend_selection(
        manager=_NoBackend(),
        local_only=True,
        # A stale/explicit override must not outrank Local Only.
        allow_remote_only=True,
    )

    assert selection.backend_name == "local_only_no_backend"
    assert selection.backend_name != "remote_only"
    decision = runtime_provider_decision(
        selection,
        local_only=True,
        allow_remote_only=True,
    )
    assert decision == {
        "requested_mode": "local_only",
        "effective_mode": "unavailable_no_local_backend",
        "selected_provider": None,
        "selected_lane": None,
        "remote_fallback_allowed": False,
        "remote_request_eligible": False,
        "local_backend_available": False,
        "unavailable_reason": "no_local_backend",
        "chat_ready": False,
        "backend_name": "local_only_no_backend",
        "backend_reason": selection.reason,
    }


def test_explicit_remote_fallback_selects_remote_when_local_is_unavailable() -> None:
    selection = resolve_backend_selection(
        manager=_NoBackend(),
        local_only=False,
        allow_remote_only=True,
    )

    assert selection.backend_name == "remote_only"
    decision = runtime_provider_decision(
        selection,
        local_only=False,
        allow_remote_only=True,
    )
    assert decision["effective_mode"] == "remote_only"
    assert decision["selected_lane"] == "remote"
    assert decision["remote_fallback_allowed"] is True
    assert decision["remote_request_eligible"] is True
    assert decision["local_backend_available"] is False


def test_hive_and_helper_flags_do_not_grant_remote_inference_authority() -> None:
    flags = RuntimeFeatureFlags(
        local_only_mode=True,
        public_hive_enabled=True,
        helper_mesh_enabled=True,
        allow_workspace_writes=False,
        allow_sandbox_execution=False,
        # Deliberately inconsistent input models stale capability state.
        allow_remote_only_without_backend=True,
    )
    statuses = {
        item.name: item
        for item in runtime_capability_statuses(SimpleNamespace(feature_flags=flags))
    }
    assert statuses["public_hive_surface"].state == "implemented"
    assert statuses["helper_mesh"].state == "implemented"
    assert statuses["remote_only_backend_fallback"].state == "disabled_by_policy"

    runtime = RuntimeServices(local_only_mode=flags.local_only_mode)
    source_context: dict[str, object] = {}
    bind_runtime_provider_policy(source_context, runtime)
    assert source_context["local_only_mode"] is True
    assert cloud_lane_permitted(source_context) is False

    remote_manifest = ModelProviderManifest(
        provider_name="remote-http",
        model_name="remote-model",
        source_type="http",
        adapter_type="openai_compatible",
        license_name="Apache-2.0",
        license_reference="https://www.apache.org/licenses/LICENSE-2.0",
        runtime_dependency="remote-provider",
        capabilities=["summarize"],
        runtime_config={"base_url": "https://provider.example"},
    )
    with pytest.raises(CloudEgressBlockedError):
        assert_manifest_allowed(remote_manifest, source_context=source_context)


def test_capability_response_exposes_the_boot_decision_not_a_recommended_profile() -> None:
    decision = {
        "requested_mode": "local_only",
        "effective_mode": "unavailable_no_local_backend",
        "selected_provider": None,
        "selected_lane": None,
        "remote_fallback_allowed": False,
        "remote_request_eligible": False,
        "local_backend_available": False,
        "unavailable_reason": "no_local_backend",
        "chat_ready": False,
    }
    runtime = RuntimeServices(
        local_only_mode=True,
        runtime_version_stamp={"provider_decision": decision},
        local_model_setup={"chat_ready": False},
    )
    payload = capability_snapshot_with_runtime(
        runtime,
        lambda: {
            "feature_flags": {
                "local_only_mode": False,
                "allow_remote_only_without_backend": True,
                "public_hive_enabled": True,
                "helper_mesh_enabled": True,
            },
            "capabilities": [
                {"name": "remote_only_backend_fallback", "state": "implemented"},
                {"name": "public_hive_surface", "state": "implemented"},
                {"name": "helper_mesh", "state": "implemented"},
            ],
        },
    )

    assert payload["provider_decision"] == decision
    assert payload["local_model_setup"]["chat_ready"] is False
    assert payload["feature_flags"]["local_only_mode"] is True
    assert payload["feature_flags"]["allow_remote_only_without_backend"] is False
    capabilities = {item["name"]: item for item in payload["capabilities"]}
    assert capabilities["remote_only_backend_fallback"]["state"] == "disabled_by_policy"
    assert capabilities["public_hive_surface"]["state"] == "implemented"
    assert capabilities["helper_mesh"]["state"] == "implemented"


def test_effective_local_decision_updates_without_rewriting_boot_evidence() -> None:
    boot = {
        "requested_mode": "local_only",
        "effective_mode": "unavailable_no_local_backend",
        "selected_provider": None,
        "selected_lane": None,
        "remote_fallback_allowed": False,
        "remote_request_eligible": False,
        "local_backend_available": False,
        "unavailable_reason": "no_local_backend",
        "chat_ready": False,
        "backend_name": "local_only_no_backend",
        "backend_reason": "no_supported_backend",
    }
    current = effective_runtime_provider_decision(
        boot,
        local_only=True,
        local_model_setup={
            "model": "qwen2.5:7b",
            "state": "ready",
            "chat_ready": True,
            "certification_state": "verified",
            "provider_state": "ready",
        },
        provider_capability_truth=(
            {
                "provider_id": "ollama-local:qwen2.5:7b",
                "availability_state": "ready",
            },
        ),
    )

    assert boot["effective_mode"] == "unavailable_no_local_backend"
    assert current["effective_mode"] == "local_only"
    assert current["selected_provider"] == "ollama-local:qwen2.5:7b"
    assert current["selected_lane"] == "local"
    assert current["local_backend_available"] is True
    assert current["remote_fallback_allowed"] is False
    assert current["remote_request_eligible"] is False
    assert current["chat_ready"] is True


def test_capability_snapshot_exposes_current_and_boot_provider_decisions() -> None:
    boot = {
        "requested_mode": "local_only",
        "effective_mode": "unavailable_no_local_backend",
        "selected_provider": None,
        "selected_lane": None,
        "remote_fallback_allowed": False,
        "remote_request_eligible": False,
        "local_backend_available": False,
        "unavailable_reason": "no_local_backend",
        "chat_ready": False,
    }
    runtime = RuntimeServices(
        local_only_mode=True,
        runtime_version_stamp={
            "provider_decision": dict(boot),
            "boot_provider_decision": dict(boot),
        },
        provider_capability_truth=(
            {
                "provider_id": "ollama-local:qwen2.5:7b",
                "availability_state": "ready",
            },
        ),
        local_model_setup={
            "model": "qwen2.5:7b",
            "state": "ready",
            "chat_ready": True,
            "certification_state": "verified",
            "provider_state": "ready",
        },
    )

    payload = capability_snapshot_with_runtime(runtime, lambda: {"capabilities": []})

    assert payload["boot_provider_decision"] == boot
    assert payload["current_provider_decision"]["selected_provider"] == "ollama-local:qwen2.5:7b"
    assert payload["provider_decision"] == payload["current_provider_decision"]
