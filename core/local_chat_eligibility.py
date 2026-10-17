"""Current-state eligibility for an installed local chat model.

Setup, capability reporting, and ordinary routing must not each invent a different
meaning of "ready".  This module owns the small, side-effect-bounded decision that a
local manifest can currently author an ordinary model-dependent answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.local_model_policy import local_models_enabled, manifest_is_local
from core.model_registry import ModelRegistry
from core.ollama_endpoint import ollama_base_url
from core.provider_routing import provider_capability_truth_for_manifest
from core.resource_governor import (
    current_runtime_identity,
    plan_model_load,
    resident_models,
)
from storage.model_provider_manifest import ModelProviderManifest


@dataclass(frozen=True)
class LocalChatEligibility:
    """The current local-chat truth shared by setup and ordinary routing."""

    eligible: bool
    state: str
    reason: str
    provider_id: str
    model_tag: str
    model_digest: str
    endpoint: str
    installed: bool
    registered: bool
    enabled: bool
    certified: bool
    resident: bool
    load_required: bool
    load_admission: str
    provider_state: str
    hardware_fit: bool | None
    hardware_fit_reason: str
    certification_state: str
    resource_decision: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "state": self.state,
            "reason": self.reason,
            "provider_id": self.provider_id,
            "model": self.model_tag,
            "model_tag": self.model_tag,
            "model_digest": self.model_digest,
            "endpoint": self.endpoint,
            "installed": self.installed,
            "registered": self.registered,
            "enabled": self.enabled,
            "certified": self.certified,
            "resident": self.resident,
            "load_required": self.load_required,
            "load_admission": self.load_admission,
            "provider_state": self.provider_state,
            "hardware_fit": self.hardware_fit,
            "hardware_fit_reason": self.hardware_fit_reason,
            "certification_state": self.certification_state,
            "resource_decision": dict(self.resource_decision),
        }


def evaluate_local_chat_eligibility(
    registry: ModelRegistry,
    *,
    model_tag: str,
    manifest: ModelProviderManifest | None = None,
    setup: Mapping[str, Any] | None = None,
    provider_capability_truth: Iterable[Mapping[str, Any] | Any] = (),
    endpoint: str | None = None,
    runtime_home: str | None = None,
    env: Mapping[str, str] | None = None,
    runtime_identity: str | None = None,
    inventory: Iterable[Any] | None = None,
    plan_loader: Any | None = None,
    residents_loader: Any | None = None,
) -> LocalChatEligibility:
    """Evaluate whether one exact local model can currently serve an ordinary turn.

    The resource governor remains the authority for cold-load admission.  This function never
    treats registration, certification, readiness, or an empty ``/api/ps`` as residency proof.
    ``plan_loader`` and ``residents_loader`` are injectable only to keep the decision deterministic
    in unit tests; production callers use the governor implementations above.
    """

    setup_map = dict(setup or {})
    plan_loader = plan_model_load if plan_loader is None else plan_loader
    residents_loader = resident_models if residents_loader is None else residents_loader
    tag = str(model_tag or setup_map.get("model") or "").strip()
    provider_id = f"ollama-local:{tag}" if tag else ""
    endpoint_value = str(endpoint or setup_map.get("ollama_endpoint") or "").strip()
    digest = str(setup_map.get("model_digest") or "").strip()
    if not tag:
        return _decision(
            state="model_missing",
            reason="no local model tag is selected",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
        )

    if not local_models_enabled(env=env, runtime_home=runtime_home):
        return _decision(
            state="policy_disabled",
            reason="local models are disabled by the current runtime policy",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
        )

    installed = _inventory_contains(tag, inventory)
    inventory_state = str(setup_map.get("inventory_state") or "").strip().lower()
    if installed is None:
        if inventory_state == "present":
            installed = True
        elif inventory_state == "missing":
            installed = False
        else:
            installed = False
    if not installed:
        return _decision(
            state="model_missing",
            reason=f"local model inventory does not contain {tag}",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=False,
        )

    manifest = manifest or _find_enabled_manifest(registry, provider_id, tag)
    if manifest is None:
        return _decision(
            state="route_manifest_unavailable",
            reason=f"enabled local route is not visible for {provider_id}",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
        )
    manifest_endpoint = str((getattr(manifest, "runtime_config", {}) or {}).get("base_url") or "").strip()
    endpoint_value = endpoint_value or manifest_endpoint
    if not _is_local_manifest(manifest):
        return _decision(
            state="route_manifest_unavailable",
            reason=f"manifest {provider_id} is not local",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
        )

    if not digest:
        digest = _manifest_identity(manifest)
    certification_state = str(
        setup_map.get("certification_state")
        or _read_certification_state(manifest)
        or "unknown"
    ).strip().lower()
    certified = certification_state == "verified"
    if not certified:
        return _decision(
            state="certification_not_verified",
            reason=f"local model certification is {certification_state}",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
            certified=False,
            certification_state=certification_state,
        )

    provider_row = _provider_row(provider_capability_truth, provider_id)
    if not provider_row:
        provider_row = provider_capability_truth_for_manifest(manifest).to_dict()
    provider_state = str(provider_row.get("availability_state") or "unknown").strip().lower()
    hardware_fit = _optional_bool(provider_row.get("hardware_fit"))
    hardware_fit_reason = str(provider_row.get("hardware_fit_reason") or "").strip()
    if provider_state != "ready" or bool(provider_row.get("circuit_open")):
        return _decision(
            state="provider_unhealthy",
            reason=f"local provider state is {provider_state or 'unknown'}",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
            certified=True,
            certification_state=certification_state,
            provider_state=provider_state,
            hardware_fit=hardware_fit,
            hardware_fit_reason=hardware_fit_reason,
        )

    current_residents = _safe_residents(residents_loader)
    resident = _resident_match(current_residents, tag, digest, provider_id)
    if not endpoint_value:
        endpoint_value = ollama_base_url(env)
    try:
        load_decision = plan_loader(
            tag,
            provider_id=provider_id,
            model_digest=digest,
            endpoint=endpoint_value,
            runtime_identity=runtime_identity or current_runtime_identity(),
        )
    except Exception as exc:
        return _decision(
            state="model_load_gated_low_memory",
            reason=f"local model load admission is unavailable: {type(exc).__name__}",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
            certified=True,
            resident=resident,
            load_required=not resident,
            load_admission="unverifiable",
            certification_state=certification_state,
            provider_state=provider_state,
            hardware_fit=hardware_fit,
            hardware_fit_reason=hardware_fit_reason,
        )
    load_payload = _decision_payload(load_decision)
    if not bool(getattr(load_decision, "ok", load_payload.get("ok", False))):
        return _decision(
            state="model_load_gated_low_memory",
            reason="local model cold-load admission refused by the resource governor",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
            certified=True,
            resident=resident,
            load_required=not resident,
            load_admission="refused",
            certification_state=certification_state,
            provider_state=provider_state,
            hardware_fit=hardware_fit,
            hardware_fit_reason=hardware_fit_reason,
            resource_decision=load_payload,
        )

    # The static hardware filter is a separate safety fence and remains authoritative even when
    # the governor has enough momentary headroom. It must never be silently bypassed here.
    if hardware_fit is False or hardware_fit_reason:
        return _decision(
            state="model_load_gated_low_memory",
            reason=hardware_fit_reason or "local model exceeds the provider hardware budget",
            provider_id=provider_id,
            model_tag=tag,
            model_digest=digest,
            endpoint=endpoint_value,
            installed=True,
            registered=True,
            enabled=bool(manifest.enabled),
            certified=True,
            resident=resident,
            load_required=not resident,
            load_admission="refused",
            certification_state=certification_state,
            provider_state=provider_state,
            hardware_fit=hardware_fit,
            hardware_fit_reason=hardware_fit_reason,
            resource_decision=load_payload,
        )

    return _decision(
        state="eligible",
        reason="local model is installed, registered, certified, healthy and load-admissible",
        provider_id=provider_id,
        model_tag=tag,
        model_digest=digest,
        endpoint=endpoint_value,
        installed=True,
        registered=True,
        enabled=bool(manifest.enabled),
        certified=True,
        resident=resident,
        load_required=not resident,
        load_admission="resident" if resident else "fit",
        certification_state=certification_state,
        provider_state=provider_state,
        hardware_fit=hardware_fit,
        hardware_fit_reason=hardware_fit_reason,
        resource_decision=load_payload,
    )


def _decision(
    *,
    state: str,
    reason: str,
    provider_id: str,
    model_tag: str,
    model_digest: str,
    endpoint: str,
    installed: bool = False,
    registered: bool = False,
    enabled: bool = False,
    certified: bool = False,
    resident: bool = False,
    load_required: bool = True,
    load_admission: str = "unknown",
    provider_state: str = "unknown",
    hardware_fit: bool | None = None,
    hardware_fit_reason: str = "",
    certification_state: str = "unknown",
    resource_decision: Mapping[str, Any] | None = None,
) -> LocalChatEligibility:
    return LocalChatEligibility(
        eligible=state == "eligible",
        state=state,
        reason=str(reason or ""),
        provider_id=provider_id,
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        installed=installed,
        registered=registered,
        enabled=enabled,
        certified=certified,
        resident=resident,
        load_required=load_required,
        load_admission=load_admission,
        provider_state=provider_state,
        hardware_fit=hardware_fit,
        hardware_fit_reason=hardware_fit_reason,
        certification_state=certification_state,
        resource_decision=dict(resource_decision or {}),
    )


def _find_enabled_manifest(
    registry: ModelRegistry,
    provider_id: str,
    model_tag: str,
) -> ModelProviderManifest | None:
    for manifest in registry.list_manifests(enabled_only=True):
        if str(getattr(manifest, "provider_id", "") or "") == provider_id and str(
            getattr(manifest, "model_name", "") or ""
        ) == model_tag:
            return manifest
    return None


def _inventory_contains(model_tag: str, inventory: Iterable[Any] | None) -> bool | None:
    if inventory is None:
        return None
    wanted = model_tag.lower()
    return any(str(getattr(item, "name", item) or "").strip().lower() == wanted for item in inventory)


def _provider_row(rows: Iterable[Mapping[str, Any] | Any], provider_id: str) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, Mapping):
            candidate = dict(row)
        else:
            to_dict = getattr(row, "to_dict", None)
            candidate = dict(to_dict()) if callable(to_dict) else dict(getattr(row, "__dict__", {}) or {})
        if str(candidate.get("provider_id") or "").strip() == provider_id:
            return candidate
    return {}


def _manifest_identity(manifest: ModelProviderManifest) -> str:
    metadata = dict(getattr(manifest, "metadata", {}) or {})
    for key in ("model_digest", "digest", "ollama_digest", "identity_fingerprint"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def _read_certification_state(manifest: ModelProviderManifest) -> str:
    try:
        from core.local_model_tool_certification import certification_status

        return str(certification_status(manifest).get("state") or "")
    except Exception:
        return ""


def _safe_residents(loader: Any) -> list[dict[str, Any]]:
    try:
        rows = loader()
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _resident_match(rows: Iterable[Mapping[str, Any]], model_tag: str, digest: str, provider_id: str) -> bool:
    for row in rows:
        if str(row.get("name") or row.get("model") or "").strip() != model_tag:
            continue
        observed_digest = str(row.get("digest") or "").strip()
        if digest and observed_digest and digest != observed_digest:
            continue
        if provider_id:
            return True
    return False


def _decision_payload(decision: Any) -> dict[str, Any]:
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {
        key: getattr(decision, key)
        for key in ("ok", "footprint_gb", "usable_before_gb", "usable_after_gb", "actions")
        if hasattr(decision, key)
    }


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _is_local_manifest(manifest: Any) -> bool:
    try:
        return bool(manifest_is_local(manifest))
    except AttributeError:
        return str(getattr(manifest, "provider_id", "") or "").startswith("ollama-local:")


__all__ = ["LocalChatEligibility", "evaluate_local_chat_eligibility"]
