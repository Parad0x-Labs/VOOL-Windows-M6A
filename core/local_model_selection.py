"""Hardware-fit local model selection for first-run provisioning.

The installer/build recommendation is only an Auto input. It is not an operator pin and it is
not permission to download a model that the runtime cannot cold-load. This module is deliberately
read-only: it evaluates catalog candidates and returns one selected tag for the existing pull,
registration, certification, and routing path to consume.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.hardware_tier import MachineProbe
from core.local_model_bundles import (
    LocalBundleRecommendation,
    model_metadata,
    model_storage_gb,
    resolve_local_bundle_recommendation,
    safe_disk_floor_gb,
)
from core.local_model_policy import resolve_local_model_policy
from core.local_specialist_lane import secondary_local_model
from core.resource_governor import GovernorDecision, cold_model_load_admission
from core.system_resources import ResourceSnapshot

AUTO_SELECTION_MODE = "auto"
EXPLICIT_SELECTION_MODE = "explicit"
_DAILY_ROLES = frozenset({"general", "daily_accelerated"})
_TINY_ROLES = frozenset({"tiny_fast"})
_ROLE_ORDER = {
    "general": 0,
    "daily_accelerated": 1,
    "tiny_fast": 2,
    "lightweight_utility": 3,
    "coding": 4,
    "reasoning": 5,
    "deep_overnight": 6,
    "heavy_reasoning": 7,
}


@dataclass(frozen=True)
class ModelCandidateEvaluation:
    model: str
    role: str
    backend: str
    result: str
    reason: str
    footprint_gb: float
    required_headroom_gb: float
    available_ram_gb: float
    disk_required_gb: float
    free_disk_gb: float
    resident: bool = False
    load_required: bool = True

    @property
    def accepted(self) -> bool:
        return self.result == "selected"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "role": self.role,
            "backend": self.backend,
            "result": self.result,
            "reason": self.reason,
            "footprint_gb": self.footprint_gb,
            "required_headroom_gb": self.required_headroom_gb,
            "available_ram_gb": self.available_ram_gb,
            "disk_required_gb": self.disk_required_gb,
            "free_disk_gb": self.free_disk_gb,
            "resident": self.resident,
            "load_required": self.load_required,
        }


@dataclass(frozen=True)
class LocalModelProvisioningSelection:
    selection_mode: str
    selected_model: str
    result: str
    reason: str
    candidates: tuple[ModelCandidateEvaluation, ...]
    free_disk_gb: float
    available_ram_gb: float
    policy_enabled: bool

    @property
    def selected(self) -> bool:
        return bool(self.selected_model) and self.result == "selected"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "vool.local_model_provisioning_selection.v1",
            "selection_mode": self.selection_mode,
            "selected_model": self.selected_model,
            "result": self.result,
            "reason": self.reason,
            "free_disk_gb": self.free_disk_gb,
            "available_ram_gb": self.available_ram_gb,
            "policy_enabled": self.policy_enabled,
            "candidates": [item.to_dict() for item in self.candidates],
        }


def resolve_model_selection_mode(
    *,
    provider_env: Mapping[str, str],
    process_env: Mapping[str, str] | None = None,
    persisted_model: str = "",
) -> str:
    """Distinguish a real operator pin from an inherited build/install recommendation.

    ``VOOL_OLLAMA_MODEL`` is written by the installer and exported by the bundle launcher. It is
    therefore an Auto recommendation unless it differs from the persisted install record or the
    bundle explicitly marks a different operator override. ``VOOL_FORCE_OLLAMA_MODEL`` remains an
    explicit pin. A direct process override with no bundle marker is also explicit.
    """
    forced = str(provider_env.get("VOOL_FORCE_OLLAMA_MODEL") or "").strip()
    if forced:
        return EXPLICIT_SELECTION_MODE

    process = process_env if process_env is not None else os.environ
    process_model = str(process.get("VOOL_OLLAMA_MODEL") or "").strip()
    if not process_model:
        return AUTO_SELECTION_MODE

    persisted = str(persisted_model or "").strip()
    bundle_model = str(
        provider_env.get("VOOL_BUNDLE_MODEL")
        or process.get("VOOL_BUNDLE_MODEL")
        or ""
    ).strip()
    if bundle_model and process_model.lower() == bundle_model.lower():
        return AUTO_SELECTION_MODE
    if persisted and process_model.lower() == persisted.lower():
        return AUTO_SELECTION_MODE
    return EXPLICIT_SELECTION_MODE


def select_local_model_for_provisioning(
    *,
    selection_mode: str,
    requested_model: str,
    probe: MachineProbe | Mapping[str, Any],
    free_disk_gb: float,
    snapshot: ResourceSnapshot | Any | None = None,
    env: Mapping[str, str] | None = None,
    runtime_home: str | Path | None = None,
    recommendation: LocalBundleRecommendation | None = None,
    supported_backends: Sequence[str] = ("ollama",),
    already_installed: bool = False,
) -> LocalModelProvisioningSelection:
    """Select one local model without downloading, registering, or certifying any candidate."""
    mode = _normalize_selection_mode(selection_mode)
    requested = str(requested_model or "").strip()
    env_map = os.environ if env is None else env
    policy = resolve_local_model_policy(env=env_map, runtime_home=runtime_home)
    active_snapshot = snapshot
    if active_snapshot is None:
        from core import system_resources as sr

        active_snapshot = sr.snapshot(force=True)
    available_ram_gb = _available_ram_gb(active_snapshot)
    free_disk = max(0.0, float(free_disk_gb or 0.0))

    if not policy.local_models_enabled:
        candidate = _candidate_evaluation(
            model=requested,
            role="general",
            backend="ollama",
            result="policy_disabled",
            reason="local_models_disabled",
            admission=None,
            free_disk_gb=free_disk,
            available_ram_gb=available_ram_gb,
        )
        return LocalModelProvisioningSelection(
            selection_mode=mode,
            selected_model="",
            result="policy_disabled",
            reason="Local models are disabled by the active local-model policy.",
            candidates=(candidate,),
            free_disk_gb=round(free_disk, 1),
            available_ram_gb=round(available_ram_gb, 1),
            policy_enabled=False,
        )

    if mode == EXPLICIT_SELECTION_MODE:
        if not requested:
            return LocalModelProvisioningSelection(
                selection_mode=mode,
                selected_model="",
                result="unsuitable_role",
                reason="An explicit local model selection was not provided.",
                candidates=(),
                free_disk_gb=round(free_disk, 1),
                available_ram_gb=round(available_ram_gb, 1),
                policy_enabled=True,
            )
        candidate = _evaluate_candidate(
            model=requested,
            role=str(model_metadata(requested).get("bundle_role") or "general").strip().lower(),
            backend="ollama",
            allowed_role=True,
            supported_backends=supported_backends,
            snapshot=active_snapshot,
            free_disk_gb=free_disk,
            available_ram_gb=available_ram_gb,
        )
        return _selection_from_candidate(mode, candidate, free_disk, available_ram_gb)

    if already_installed and requested:
        candidate = _candidate_evaluation(
            model=requested,
            role=str(model_metadata(requested).get("bundle_role") or "general").strip().lower(),
            backend="ollama",
            result="selected",
            reason="already installed; retain it and let ordinary runtime admission govern loading",
            admission=None,
            free_disk_gb=free_disk,
            available_ram_gb=available_ram_gb,
        )
        return LocalModelProvisioningSelection(
            selection_mode=mode,
            selected_model=requested,
            result="selected",
            reason=(
                f"Retained installed Auto model `{requested}`; first-run provisioning is complete "
                "and ordinary runtime admission remains authoritative for loading."
            ),
            candidates=(candidate,),
            free_disk_gb=round(free_disk, 1),
            available_ram_gb=round(available_ram_gb, 1),
            policy_enabled=True,
        )

    if recommendation is None and not _probe_has_catalog_inputs(probe):
        # Unit/integration callers may provide a deliberately partial probe. Do not invent a
        # hardware tier or a fallback model when the hardware authority is unavailable; apply
        # the same cold-load gate to the requested Auto input and fail closed if it does not fit.
        candidates = (
            _CandidateSpec(
                requested,
                str(model_metadata(requested).get("bundle_role") or "general").strip().lower(),
                "ollama",
            ),
        ) if requested else ()
    else:
        recommendation = recommendation or resolve_local_bundle_recommendation(
            probe=probe,
            free_disk_gb=free_disk,
            secondary_local_model_name=secondary_local_model(env_map),
        )
        candidates = _auto_candidate_specs(requested, recommendation)
    evaluations: list[ModelCandidateEvaluation] = []
    for spec in candidates:
        candidate = _evaluate_candidate(
            model=spec.model,
            role=spec.role,
            backend=spec.backend,
            allowed_role=spec.role in _DAILY_ROLES or spec.role in _TINY_ROLES,
            supported_backends=supported_backends,
            snapshot=active_snapshot,
            free_disk_gb=free_disk,
            available_ram_gb=available_ram_gb,
        )
        evaluations.append(candidate)
        if candidate.accepted:
            return LocalModelProvisioningSelection(
                selection_mode=mode,
                selected_model=candidate.model,
                result="selected",
                reason=(
                    f"Selected `{candidate.model}` as the highest-preference {candidate.role} "
                    "local candidate that passes the cold-load and disk gates."
                ),
                candidates=tuple(evaluations),
                free_disk_gb=round(free_disk, 1),
                available_ram_gb=round(available_ram_gb, 1),
                policy_enabled=True,
            )

    final_result = evaluations[-1].result if evaluations else "unsuitable_role"
    return LocalModelProvisioningSelection(
        selection_mode=mode,
        selected_model="",
        result=final_result,
        reason=(
            "No supported daily/general local model passed the cold-load and disk gates; "
            "no model download was started."
        ),
        candidates=tuple(evaluations),
        free_disk_gb=round(free_disk, 1),
        available_ram_gb=round(available_ram_gb, 1),
        policy_enabled=True,
    )


def free_disk_gb_for_path(path: str | Path) -> float:
    """Read free space without creating the model-store path."""
    current = Path(path).expanduser()
    while not current.exists() and current != current.parent:
        current = current.parent
    try:
        return max(0.0, float(shutil.disk_usage(current).free) / (1024.0**3))
    except OSError:
        return 0.0


@dataclass(frozen=True)
class _CandidateSpec:
    model: str
    role: str
    backend: str


def _auto_candidate_specs(
    requested_model: str,
    recommendation: LocalBundleRecommendation,
) -> tuple[_CandidateSpec, ...]:
    specs: list[_CandidateSpec] = []
    seen: set[str] = set()

    def add(model: str, role: str = "general", backend: str = "ollama") -> None:
        clean = str(model or "").strip()
        key = clean.lower()
        if not clean or key in seen:
            return
        seen.add(key)
        specs.append(_CandidateSpec(clean, str(role or "general").strip().lower(), str(backend or "ollama")))

    if requested_model:
        add(requested_model, _bundle_role_for_model(requested_model, recommendation))

    role_models = [
        *recommendation.recommended_bundle.role_models,
        *recommendation.fallback_bundle.role_models,
    ]
    role_models.sort(key=lambda item: _ROLE_ORDER.get(str(item.role or "").strip().lower(), 99))
    for item in role_models:
        add(item.model, item.role, item.backend)
    return tuple(specs)


def _bundle_role_for_model(model: str, recommendation: LocalBundleRecommendation) -> str:
    target = str(model or "").strip().lower()
    for spec in (recommendation.recommended_bundle, recommendation.fallback_bundle):
        for item in spec.role_models:
            if str(item.model or "").strip().lower() == target:
                return str(item.role or "general").strip().lower() or "general"
    return str(model_metadata(model).get("bundle_role") or "general").strip().lower() or "general"


def _evaluate_candidate(
    *,
    model: str,
    role: str,
    backend: str,
    allowed_role: bool,
    supported_backends: Sequence[str],
    snapshot: ResourceSnapshot | Any,
    free_disk_gb: float,
    available_ram_gb: float,
) -> ModelCandidateEvaluation:
    if not allowed_role:
        return _candidate_evaluation(
            model=model,
            role=role,
            backend=backend,
            result="unsuitable_role",
            reason=f"role `{role}` is not a daily/general provisioning lane",
            admission=None,
            free_disk_gb=free_disk_gb,
            available_ram_gb=available_ram_gb,
        )
    if str(backend or "").strip().lower() not in {str(item).strip().lower() for item in supported_backends}:
        return _candidate_evaluation(
            model=model,
            role=role,
            backend=backend,
            result="backend_unsupported",
            reason=f"backend `{backend}` is not available for this provisioning path",
            admission=None,
            free_disk_gb=free_disk_gb,
            available_ram_gb=available_ram_gb,
        )

    admission = cold_model_load_admission(model, snapshot=snapshot)
    if not admission.ok:
        return _candidate_evaluation(
            model=model,
            role=role,
            backend=backend,
            result="model_load_gated_low_memory",
            reason=(
                f"cold load requires {admission.footprint_gb + _safety_floor_gb():.1f} GiB headroom; "
                f"available {available_ram_gb:.1f} GiB"
            ),
            admission=admission,
            free_disk_gb=free_disk_gb,
            available_ram_gb=available_ram_gb,
        )

    disk_required = safe_disk_floor_gb((model,))
    if free_disk_gb < disk_required:
        return _candidate_evaluation(
            model=model,
            role=role,
            backend=backend,
            result="insufficient_disk",
            reason=(
                f"model store requires {disk_required:.1f} GiB free for `{model}`; "
                f"available {free_disk_gb:.1f} GiB"
            ),
            admission=admission,
            free_disk_gb=free_disk_gb,
            available_ram_gb=available_ram_gb,
        )
    return _candidate_evaluation(
        model=model,
        role=role,
        backend=backend,
        result="selected",
        reason="cold-load and disk admission passed",
        admission=admission,
        free_disk_gb=free_disk_gb,
        available_ram_gb=available_ram_gb,
    )


def _candidate_evaluation(
    *,
    model: str,
    role: str,
    backend: str,
    result: str,
    reason: str,
    admission: GovernorDecision | None,
    free_disk_gb: float,
    available_ram_gb: float,
) -> ModelCandidateEvaluation:
    footprint = float(admission.footprint_gb if admission is not None else model_storage_gb(model))
    return ModelCandidateEvaluation(
        model=str(model or "").strip(),
        role=str(role or "general").strip().lower(),
        backend=str(backend or "ollama").strip().lower(),
        result=str(result or "other"),
        reason=str(reason or ""),
        footprint_gb=round(footprint, 1),
        required_headroom_gb=round(footprint + _safety_floor_gb(), 1),
        available_ram_gb=round(available_ram_gb, 1),
        disk_required_gb=round(safe_disk_floor_gb((model,)), 1),
        free_disk_gb=round(free_disk_gb, 1),
        resident=False,
        load_required=True,
    )


def _selection_from_candidate(
    mode: str,
    candidate: ModelCandidateEvaluation,
    free_disk_gb: float,
    available_ram_gb: float,
) -> LocalModelProvisioningSelection:
    return LocalModelProvisioningSelection(
        selection_mode=mode,
        selected_model=candidate.model if candidate.accepted else "",
        result=candidate.result,
        reason=(
            f"Explicit model `{candidate.model}` is accepted without substitution."
            if candidate.accepted
            else f"Explicit model `{candidate.model}` was refused: {candidate.reason}. No substitute was selected."
        ),
        candidates=(candidate,),
        free_disk_gb=round(free_disk_gb, 1),
        available_ram_gb=round(available_ram_gb, 1),
        policy_enabled=True,
    )


def _normalize_selection_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {EXPLICIT_SELECTION_MODE, "pinned", "explicit/pinned"}:
        return EXPLICIT_SELECTION_MODE
    return AUTO_SELECTION_MODE


def _available_ram_gb(snapshot: Any) -> float:
    try:
        return max(0.0, float(getattr(snapshot, "available_ram_gb", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _probe_has_catalog_inputs(probe: MachineProbe | Mapping[str, Any]) -> bool:
    try:
        raw_ram = probe.get("ram_gb") if isinstance(probe, Mapping) else getattr(probe, "ram_gb", None)
        ram_gb = float(raw_ram or 0.0)
    except (TypeError, ValueError):
        return False
    return ram_gb > 0.0


def _safety_floor_gb() -> float:
    # Keep the selector's explanatory values tied to the governor's live configured floor.
    from core.resource_governor import _LOAD_SAFETY_FLOOR_GB

    return float(_LOAD_SAFETY_FLOOR_GB)


__all__ = [
    "AUTO_SELECTION_MODE",
    "EXPLICIT_SELECTION_MODE",
    "LocalModelProvisioningSelection",
    "ModelCandidateEvaluation",
    "free_disk_gb_for_path",
    "resolve_model_selection_mode",
    "select_local_model_for_provisioning",
]
