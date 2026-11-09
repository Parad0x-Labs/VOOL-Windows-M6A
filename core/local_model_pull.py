"""A user-requested local model download: one click, one offered model, nothing automatic.

Clean-VM job 52 (port 76afbd36): a new Windows user could not get a model from VOOL. The only pull is
the boot pull (``core.web.api.runtime.start_ollama_model_pull``), which is skipped while
``network.outbound_enabled`` keeps its default of false, so the app told the user to open a terminal.
Owner decision (Jesse, 7 Nov 2026 05:14Z): "click-to-download counts as consent". Security review
(🔐, design D2) bound that consent:

* Only a name:tag the machine-sized install plan offers (``build_install_recommendation_truth``:
  the recommended bundle, then its fallback, led by the bundle manifest's own model when the machine
  meets its documented RAM floor) is pulled, matched exactly. There is no free text and
  no host or registry field; the pull goes to the local Ollama only, which fetches from its registry.
* Local-only (offline) mode and a local-models-disabled policy win over the click. The click only
  stands in for the default ``outbound_enabled=false``, for this one pull, and never changes it.
* One pull at a time; Cancel closes the stream so Ollama stops; the disk check runs first, against
  the same safe floor the install plan uses (``safe_disk_floor_gb``).
* Each pull is audited as ``model_pull_user_requested`` with the model, its size and the outcome.

The boot pull is unchanged. Progress is published where Setup and Settings already read the boot
pull: ``runtime.runtime_version_stamp["model_pull"]``.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any
from urllib.parse import urlsplit

from core import audit_logger, policy_engine

SOURCE = "Ollama's registry (registry.ollama.ai), through the Ollama on this computer"
AUDIT_EVENT = "model_pull_user_requested"

_lock = threading.Lock()
_active: dict[str, Any] | None = None   # {"progress", "cancel", "thread", "model", "size_gb"}
_last: dict[str, Any] | None = None


# The RAM a CPU-only PC needs to run a bundle's certified local author, as the install guide states it
# (docs/getting-started/install.md: "qwen3:8b ... needs at least 10 GB of RAM on a PC without a
# graphics card"). A model not listed here has no documented floor and is never added to the offer.
_BUNDLE_AUTHOR_MIN_RAM_GB: dict[str, float] = {"qwen3:8b": 10.0}


def _bundle_author(ram_gb: float) -> str:
    """The installed bundle's own model (bundle_manifest.json ``selected_model``) when this machine meets
    its documented RAM floor, else empty.

    Capability tests A7/D1/D2: on a 16 GB CPU-only Windows PC the hardware bucket alone offered
    qwen3:0.6b, qwen3:4b and gemma3:4b, never qwen3:8b -- the model the bundle ships for, the one
    build_bundle.ps1 names the certified local author, and the one the runtime defaults to
    (core.runtime_provider_defaults.default_runtime_model_tag). The name comes from the immutable
    bundle manifest, never from the request, so the click door still matches a fixed plan exactly.
    """
    from core.bundle_manifest import selected_bundle_model

    try:
        model = str(selected_bundle_model() or "").strip()
    except Exception:
        return ""
    floor = _BUNDLE_AUTHOR_MIN_RAM_GB.get(model)
    if floor is None or float(ram_gb or 0.0) < floor:
        return ""
    return model


def _plan() -> tuple[tuple[str, ...], float]:
    """The offered names (the bundle's author when it fits, then the recommended bundle, then its
    fallback) and the free space where models go."""
    from core.hardware_tier import probe_machine
    from core.install_recommendations import build_install_recommendation_truth

    probe = probe_machine()
    truth = build_install_recommendation_truth(probe=probe)
    names: list[str] = []
    for name in (_bundle_author(probe.ram_gb), *truth.recommended_bundle_models, *truth.fallback_bundle_models):
        clean = str(name or "").strip()
        if clean and clean not in names:
            names.append(clean)
    return tuple(names), float(truth.free_disk_gb or 0.0)


def _size_gb(model: str) -> float:
    from core.local_model_bundles import model_storage_gb

    return float(model_storage_gb(model))


def _needed_gb(model: str) -> float:
    from core.local_model_bundles import safe_disk_floor_gb

    return float(safe_disk_floor_gb((model,)))


def _local_models_disabled() -> bool:
    from core.local_model_policy import resolve_local_model_policy

    return bool(resolve_local_model_policy().local_models_disabled)


def _base_url() -> str:
    from core.web.api.runtime import ollama_base_url

    return ollama_base_url()


def _installed(base_url: str) -> set[str]:
    from core.web.api.runtime import installed_ollama_model_names, normalized_ollama_tag

    return {normalized_ollama_tag(name) for name in installed_ollama_model_names(base_url=base_url, timeout_seconds=5.0)}


def _register(model: str) -> None:
    """Register the downloaded model as a lane, through the same factory as the Register button."""
    from core.model_registry import ModelRegistry
    from core.runtime_provider_defaults import register_installed_local_model

    register_installed_local_model(ModelRegistry(), model_tag=model)


def _audit(model: str, size_gb: float, outcome: str, detail: str = "") -> None:
    # An audit failure must not leave the pull unreported to the user.
    with contextlib.suppress(Exception):
        audit_logger.log(
            AUDIT_EVENT,
            target_id=model,
            target_type="local_model",
            actor="owner",
            details={"model": model, "size_gb": size_gb, "outcome": outcome, "detail": detail[:200],
                     "source": SOURCE},
        )


def _refusal(status: int, error: str, message: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error": error, "message": message, **extra}


def offered() -> dict[str, Any]:
    names, free_gb = _plan()
    try:
        installed = _installed(_base_url())
    except Exception:
        installed = set()
    from core.web.api.runtime import normalized_ollama_tag

    return {
        "models": [
            {"model": name, "size_gb": _size_gb(name), "installed": normalized_ollama_tag(name) in installed}
            for name in names
        ],
        "free_disk_gb": round(free_gb, 1),
        "source": SOURCE,
    }


def status() -> dict[str, Any] | None:
    with _lock:
        current = _active or _last
        return dict(current["progress"].public) if current else None


def start(model: Any, *, runtime: Any = None) -> tuple[int, dict[str, Any]]:
    """Start the one pull this click asked for, or refuse with a typed reason. Never blocks on the pull."""
    global _active
    names, free_gb = _plan()
    if not isinstance(model, str) or model not in names:
        return _refusal(400, "model_not_offered", "That model is not one VOOL offers for this computer.")
    if policy_engine.local_only_mode():
        return _refusal(409, "offline_mode", "VOOL is in local-only mode, so it downloads nothing.")
    if _local_models_disabled():
        return _refusal(409, "local_models_disabled", "Local models are turned off on this computer.")
    base_url = _base_url()
    from core.runtime_guard import is_loopback_host

    if not is_loopback_host(str(urlsplit(base_url).hostname or "")):
        return _refusal(409, "ollama_not_local", "VOOL only downloads into the Ollama on this computer.")
    try:
        installed = _installed(base_url)
    except Exception:  # an unanswered inventory is "not installed"; a dead Ollama fails the pull itself
        installed = set()
    from core.web.api.runtime import ModelPullProgress, normalized_ollama_tag

    if normalized_ollama_tag(model) in installed:
        return 200, {"ok": True, "status": "ready", "model": model}
    size_gb = _size_gb(model)
    needed_gb = _needed_gb(model)
    if free_gb < needed_gb:
        return _refusal(507, "insufficient_space", "There is not enough free disk space for this model.",
                        needed_gb=round(needed_gb, 1), free_gb=round(free_gb, 1))
    with _lock:
        if _active is not None:
            return _refusal(409, "pull_in_progress", "A model download is already running.",
                            model=_active["model"])
        progress = ModelPullProgress(model, env={"VOOL_RAW_OLLAMA_API_URL": base_url})
        progress.update(status="pulling", percent=0, detail="starting download")
        cancel_event = threading.Event()
        job = {"progress": progress, "cancel": cancel_event, "model": model, "size_gb": size_gb}
        _active = job
    if runtime is not None and isinstance(getattr(runtime, "runtime_version_stamp", None), dict):
        runtime.runtime_version_stamp["model_pull"] = progress.public
    _audit(model, size_gb, "started")
    thread = threading.Thread(target=_run, args=(job,), name="vool-user-model-pull", daemon=True)
    job["thread"] = thread
    thread.start()
    return 202, {"ok": True, "status": "pulling", "model": model, "size_gb": size_gb, "source": SOURCE}


def _run(job: dict[str, Any]) -> None:
    global _active, _last
    from core.web.api.runtime import ModelPullCancelled, _stream_ollama_pull

    progress, model, size_gb = job["progress"], job["model"], job["size_gb"]
    try:
        _stream_ollama_pull(model, progress=progress, cancelled=job["cancel"].is_set)
        if job["cancel"].is_set():
            raise ModelPullCancelled(model)
        try:
            _register(model)
        except Exception as exc:  # downloaded but not registered: still usable from Settings
            progress.update(detail=f"downloaded; register it in Settings ({exc})")
        progress.update(status="ready", percent=100, detail="download complete")
        _audit(model, size_gb, "succeeded")
    except ModelPullCancelled:
        progress.update(status="cancelled", detail="download cancelled")
        _audit(model, size_gb, "cancelled")
    except Exception as exc:
        progress.update(status="failed", detail=str(exc)[:200])
        _audit(model, size_gb, "failed", str(exc))
    finally:
        # Free the slot before announcing the end, so whoever waits on it can start the next pull.
        with _lock:
            _last = job
            if _active is job:
                _active = None
        progress.finish()


def cancel() -> tuple[int, dict[str, Any]]:
    with _lock:
        job = _active
    if job is None:
        return _refusal(409, "no_pull_running", "No model download is running.")
    job["cancel"].set()
    return 200, {"ok": True, "status": "cancelling", "model": job["model"]}


def _wait_for_tests(timeout: float) -> bool:
    with _lock:
        job = _active or _last
    if job is None:
        return True
    return bool(job["progress"].wait(timeout))


def _reset_for_tests() -> None:
    global _active, _last
    with _lock:
        _active = None
        _last = None
