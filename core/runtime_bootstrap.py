from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import policy_engine
from core.backend_manager import BackendManager
from core.logging_config import setup_logging
from core.runtime_context import RuntimeContext, apply_runtime_context, build_runtime_context
from core.runtime_paths import ensure_runtime_dirs
from storage.db import healthcheck
from storage.migrations import run_migrations


@dataclass(frozen=True)
class RuntimeBackendSelection:
    backend_name: str
    device: str
    reason: str
    hardware: Any


@dataclass(frozen=True)
class BootstrappedRuntime:
    context: RuntimeContext
    backend_selection: RuntimeBackendSelection | None = None


def runtime_provider_decision(
    selection: RuntimeBackendSelection,
    *,
    local_only: bool,
    allow_remote_only: bool,
) -> dict[str, Any]:
    """Serialize the resolved boot choice without selecting a provider prematurely."""
    local_only_requested = bool(local_only)
    remote_fallback_allowed = bool(allow_remote_only) and not local_only_requested
    backend_name = str(selection.backend_name or "").strip()
    local_backend_available = backend_name not in {
        "NoBackend",
        "local_only_no_backend",
        "remote_only",
    }

    if backend_name == "remote_only" and remote_fallback_allowed:
        effective_mode = "remote_only"
        selected_lane: str | None = "remote"
        unavailable_reason = None
        chat_ready: bool | None = None
    elif local_backend_available:
        effective_mode = "local_only" if local_only_requested else "local"
        selected_lane = "local"
        unavailable_reason = None
        chat_ready = None
    else:
        effective_mode = "unavailable_no_local_backend"
        selected_lane = None
        unavailable_reason = "no_local_backend"
        chat_ready = False

    return {
        "requested_mode": "local_only" if local_only_requested else "automatic",
        "effective_mode": effective_mode,
        # Provider selection happens per turn, after boot. `selected_lane` records only the
        # boot-level local/remote/unavailable decision; no concrete provider is invented here.
        "selected_provider": None,
        "selected_lane": selected_lane,
        "remote_fallback_allowed": remote_fallback_allowed,
        "remote_request_eligible": not local_only_requested,
        "local_backend_available": local_backend_available,
        "unavailable_reason": unavailable_reason,
        "chat_ready": chat_ready,
        "backend_name": backend_name,
        "backend_reason": str(selection.reason or ""),
    }


def effective_runtime_provider_decision(
    boot_decision: dict[str, Any] | None,
    *,
    local_only: bool,
    local_model_setup: dict[str, Any] | None,
    provider_capability_truth: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Project current provider truth without rewriting the immutable boot decision.

    Boot selects a backend before the local model is registered and certified. The effective
    decision becomes concrete only after the selected local lane is ready. Until then, returning a
    copy of the boot decision preserves the existing unavailable/starting contract.
    """

    decision = dict(boot_decision or {})
    setup = dict(local_model_setup or {})
    if (
        str(setup.get("state") or "").strip().lower() != "ready"
        or not bool(setup.get("chat_ready"))
        or str(setup.get("certification_state") or "").strip().lower() != "verified"
    ):
        return decision

    model_tag = str(setup.get("model") or "").strip()
    provider_id = f"ollama-local:{model_tag}" if model_tag else ""
    lane = next(
        (
            dict(item)
            for item in tuple(provider_capability_truth or ())
            if isinstance(item, dict)
            and str(item.get("provider_id") or "").strip() == provider_id
        ),
        {},
    )
    provider_state = str(
        setup.get("provider_state") or lane.get("availability_state") or ""
    ).strip().lower()
    if not provider_id or provider_state != "ready":
        return decision

    local_only_active = bool(local_only)
    return {
        **decision,
        "requested_mode": "local_only" if local_only_active else str(
            decision.get("requested_mode") or "automatic"
        ),
        "effective_mode": "local_only" if local_only_active else "local",
        "selected_provider": provider_id,
        "selected_lane": "local",
        "remote_fallback_allowed": (
            False if local_only_active else bool(decision.get("remote_fallback_allowed"))
        ),
        "remote_request_eligible": not local_only_active,
        "local_backend_available": True,
        "unavailable_reason": None,
        "chat_ready": True,
        "backend_name": provider_id,
        "backend_reason": "local backend registered, certified, and chat-ready",
    }


def bootstrap_storage_environment(
    *,
    context: RuntimeContext | None = None,
    db_path: str | Path | None = None,
) -> RuntimeContext:
    runtime_context = apply_runtime_context(
        context or build_runtime_context(mode="storage", db_path=db_path)
    )
    ensure_runtime_dirs()
    run_migrations(runtime_context.paths.db_path)
    healthy = healthcheck(runtime_context.paths.db_path)
    if not healthy:
        raise RuntimeError("Database healthcheck failed.")
    return runtime_context


def bootstrap_runtime_environment(
    *,
    context: RuntimeContext | None = None,
    force_policy_reload: bool = False,
) -> RuntimeContext:
    runtime_context = bootstrap_storage_environment(context=context)
    policy_engine.load(force_reload=force_policy_reload)
    return runtime_context


def bootstrap_runtime_mode(
    *,
    mode: str,
    workspace_root: str | Path | None = None,
    db_path: str | Path | None = None,
    force_policy_reload: bool = False,
    configure_logging: bool = False,
    resolve_backend: bool = False,
    manager: BackendManager | None = None,
    allow_remote_only: bool | None = None,
) -> BootstrappedRuntime:
    context = build_runtime_context(
        mode=mode,
        workspace_root=workspace_root,
        db_path=db_path,
    )
    bootstrap_runtime_environment(context=context, force_policy_reload=force_policy_reload)
    if configure_logging:
        setup_logging(level=context.log_level, json_output=context.json_logs)
    backend_selection = (
        resolve_backend_selection(
            manager=manager,
            local_only=context.feature_flags.local_only_mode,
            allow_remote_only=(
                context.feature_flags.allow_remote_only_without_backend
                if allow_remote_only is None
                else allow_remote_only
            ),
        )
        if resolve_backend
        else None
    )
    return BootstrappedRuntime(context=context, backend_selection=backend_selection)


def resolve_backend_selection(
    *,
    manager: BackendManager | None = None,
    local_only: bool | None = None,
    allow_remote_only: bool | None = None,
) -> RuntimeBackendSelection:
    local_only_active = (
        bool(policy_engine.local_only_mode()) if local_only is None else bool(local_only)
    )
    requested_remote_fallback = (
        bool(policy_engine.allow_remote_only_without_backend())
        if allow_remote_only is None
        else bool(allow_remote_only)
    )
    remote_allowed = requested_remote_fallback and not local_only_active
    backend_manager = manager or BackendManager()
    hardware = backend_manager.detect_hardware()
    selection = backend_manager.select_backend(hardware)
    if backend_manager.healthcheck(selection):
        return RuntimeBackendSelection(
            backend_name=str(selection.backend_name),
            device=str(selection.device),
            reason=str(getattr(selection, "reason", "") or ""),
            hardware=hardware,
        )

    if not remote_allowed:
        # LOCAL ONLY MUST NOT MAKE THE APP UNSTARTABLE.
        #
        # `policy_engine.allow_remote_only_without_backend()` is
        # `... and not local_only_mode()`, so turning Local Only on withdraws the remote-only
        # fallback by design. That is correct. What was NOT correct is the consequence: on a
        # machine with no healthy local backend the daemon then died at boot with a bare
        # "No supported backend found." -- and since the pact offers Local Only as a FIRST-RUN
        # choice, an operator could take it and never start the app again. The crash removed the
        # only surface (Settings, the pact) from which the setting could be turned back off.
        #
        # Measured on the packaged bundle: fresh home boots remote_only; enable
        # `local_only_composite`; the next start raises here and the app is bricked.
        #
        # Booting is survivable and already proven: in exactly this state the runtime answers
        # deterministic turns and returns an honest typed refusal for anything needing a live
        # lookup. So boot, name the condition, and leave the operator a door. A turn that needs
        # a model still refuses honestly -- this widens nothing about what may be ANSWERED, only
        # about what may be REACHED.
        # Installation profiles and saved policy share the context flag at boot.
        # Direct callers retain the policy default; neither path enables inference.
        if local_only_active:
            return RuntimeBackendSelection(
                backend_name="local_only_no_backend",
                device="cpu",
                reason=(
                    "Local Only is on and no local model backend is reachable. The app is "
                    "serving so this can be changed: install or start a local model, or turn "
                    "Local Only off in Settings."
                ),
                hardware=hardware,
            )
        raise RuntimeError("No supported backend found.")

    return RuntimeBackendSelection(
        backend_name="remote_only",
        device="cpu",
        reason=str(getattr(selection, "reason", "") or "no_supported_backend"),
        hardware=hardware,
    )
