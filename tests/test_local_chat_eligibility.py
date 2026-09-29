from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from core.agent_runtime.agent import VoolAgent
from core.local_chat_eligibility import evaluate_local_chat_eligibility
from core.runtime_bootstrap import effective_runtime_provider_decision
from core.web.api.runtime import bind_runtime_provider_policy
from storage.model_provider_manifest import ModelProviderManifest


class _Registry:
    def __init__(self, *manifests: ModelProviderManifest) -> None:
        self.manifests = list(manifests)

    def list_manifests(self, *, enabled_only: bool = False, limit: int = 256):
        rows = [item for item in self.manifests if not enabled_only or item.enabled]
        return rows[:limit]


def _manifest() -> ModelProviderManifest:
    return ModelProviderManifest(
        provider_name="ollama-local",
        model_name="qwen2.5:7b",
        source_type="http",
        adapter_type="local_qwen_provider",
        enabled=True,
        runtime_config={"base_url": "http://127.0.0.1:11434"},
        metadata={"deployment_class": "local", "model_digest": "sha256:qwen"},
    )


def _setup(**overrides):
    return {
        "model": "qwen2.5:7b",
        "inventory_state": "present",
        "certification_state": "verified",
        "provider_state": "ready",
        **overrides,
    }


def _truth(**overrides):
    return (
        {
            "provider_id": "ollama-local:qwen2.5:7b",
            "availability_state": "ready",
            "hardware_fit": True,
            **overrides,
        },
    )


def _decision(ok: bool):
    return SimpleNamespace(
        ok=ok,
        footprint_gb=8.0,
        usable_before_gb=12.0,
        usable_after_gb=12.0,
        actions=[],
    )


def _eligible(registry, *, residents=(), decision=True, setup=None):
    return evaluate_local_chat_eligibility(
        registry,
        model_tag="qwen2.5:7b",
        setup=setup or _setup(),
        provider_capability_truth=_truth(),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
        runtime_home="C:/disposable-vool-home",
        inventory=(SimpleNamespace(name="qwen2.5:7b"),),
        residents_loader=lambda: list(residents),
        plan_loader=lambda _model, **_kwargs: _decision(decision),
    )


def test_boot_without_model_becomes_eligible_after_install_without_restart() -> None:
    registry = _Registry()
    before = evaluate_local_chat_eligibility(
        registry,
        model_tag="qwen2.5:7b",
        setup=_setup(inventory_state="missing", certification_state="unknown"),
        provider_capability_truth=_truth(),
        env={"VOOL_LOCAL_MODELS_ENABLED": "1"},
        runtime_home="C:/disposable-vool-home",
        inventory=(),
        residents_loader=lambda: [],
        plan_loader=lambda _model, **_kwargs: _decision(True),
    )
    assert before.state == "model_missing"

    registry.manifests.append(_manifest())
    after = _eligible(registry)
    assert after.eligible is True
    assert after.state == "eligible"


def test_certified_nonresident_model_is_chat_ready_when_cold_load_fits() -> None:
    decision = _eligible(_Registry(_manifest()))
    assert decision.eligible is True
    assert decision.resident is False
    assert decision.load_required is True
    assert decision.load_admission == "fit"


def test_certified_nonresident_model_is_not_chat_ready_when_load_is_refused() -> None:
    decision = _eligible(_Registry(_manifest()), decision=False)
    assert decision.eligible is False
    assert decision.state == "model_load_gated_low_memory"
    assert decision.load_admission == "refused"


def test_registry_disagreement_never_reports_ready() -> None:
    decision = _eligible(_Registry())
    assert decision.eligible is False
    assert decision.state == "route_manifest_unavailable"


def test_local_only_current_decision_overrides_stale_boot_remote_truth() -> None:
    boot = {
        "requested_mode": "automatic",
        "effective_mode": "remote_only",
        "selected_provider": "remote-http:model",
        "selected_lane": "remote",
        "remote_fallback_allowed": True,
        "remote_request_eligible": True,
        "local_backend_available": False,
        "unavailable_reason": None,
        "chat_ready": False,
    }
    current = effective_runtime_provider_decision(
        boot,
        local_only=True,
        local_model_setup={"state": "failed", "chat_ready": False},
    )
    assert boot["remote_request_eligible"] is True
    assert current["requested_mode"] == "local_only"
    assert current["remote_fallback_allowed"] is False
    assert current["remote_request_eligible"] is False


def test_boot_history_remains_separate_after_local_setup_becomes_eligible() -> None:
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
    current = effective_runtime_provider_decision(
        boot,
        local_only=True,
        local_model_setup={
            **_setup(),
            "state": "ready",
            "chat_ready": True,
            "eligibility_state": "eligible",
        },
        provider_capability_truth=_truth(),
    )
    assert boot["effective_mode"] == "unavailable_no_local_backend"
    assert current["selected_provider"] == "ollama-local:qwen2.5:7b"
    assert current["chat_ready"] is True


def test_unload_keeps_an_installed_model_cold_loadable_when_memory_still_fits() -> None:
    manifest = _manifest()
    registry = _Registry(manifest)
    resident = _eligible(
        registry,
        residents=({"name": "qwen2.5:7b", "digest": "sha256:qwen"},),
    )
    unloaded = _eligible(registry, residents=())
    assert resident.eligible is True and resident.resident is True
    assert unloaded.eligible is True and unloaded.resident is False
    assert unloaded.load_required is True


def test_vool_agent_and_router_share_the_runtime_registry() -> None:
    registry = _Registry()

    def provider(*_args, **_kwargs):
        return None

    agent = VoolAgent(
        backend_name="local_only_no_backend",
        device="cpu",
        registry=registry,
        local_chat_eligibility_provider=provider,
    )
    assert agent.memory_router.registry is registry
    assert agent.memory_router._local_chat_eligibility_provider is provider


def test_runtime_policy_binding_reads_the_current_merged_profile_environment() -> None:
    runtime = SimpleNamespace(runtime_home="C:/disposable-vool-home", local_only_mode=False)
    source_context: dict[str, object] = {}
    merged_env = {"VOOL_LOCAL_MODELS_ENABLED": "1", "VOOL_LOCAL_ONLY": "1"}

    with mock.patch("core.web.api.runtime.merge_provider_env", return_value=merged_env), mock.patch(
        "core.web.api.runtime.current_local_only_mode", return_value=True
    ) as current_mode:
        bind_runtime_provider_policy(source_context, runtime)

    current_mode.assert_called_once_with(runtime_home="C:/disposable-vool-home", env=merged_env)
    assert runtime.local_only_mode is True
    assert source_context["local_only_mode"] is True
