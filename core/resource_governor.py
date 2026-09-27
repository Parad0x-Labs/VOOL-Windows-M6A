"""The resource governor — turn live sensing (system_resources) into safe action.

Policy (chosen by the owner): **auto-manage, ask only on real tradeoffs**, and always keep ~20% of
RAM free for the user (system_resources.reserve_gb). So before a heavy task (an SDXL render, a big
model load) the governor:

1. Reads live headroom (`usable_gb` = available − your reserve).
2. If the task fits, runs it — no prompt.
3. If not, RECLAIMS silently: unload Ollama's idle model (it reloads on the next chat), and stop an
   idle ComfyUI, giving those GBs back. Re-checks.
4. Only if it still will not fit does it surface a real tradeoff to the caller (run on the paid
   cloud, or a smaller image) — never a freeze.

Every operation is best-effort and loopback-only; a failure degrades to "could not reclaim", never
an exception. This never kills a process that is mid-work by force — it asks Ollama to unload and
sends ComfyUI a graceful terminate.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core import system_resources as sr

# A full SDXL render (model ~4.9 GB + VAE/CLIP + activations) — conservative so we reclaim/ask rather
# than freeze. Overridable for other pipelines.
RENDER_FOOTPRINT_GB = float(os.environ.get("VOOL_RENDER_FOOTPRINT_GB", "5.5") or 5.5)

# Adaptive render tiers, best-first. The governor picks the best that fits so an image ALWAYS renders
# on a constrained Mac — smaller + ComfyUI low-memory mode, a bit slower — instead of being refused.
# `min_available` is REAL free RAM the tier needs (its peak footprint + a small safety floor): a render
# the user asked for is a transient foreground task, so it may dip into the ambient 20% reserve rather
# than being blocked by it. `low_memory` is the ComfyUI flag ("" full speed keeps the model resident,
# "lowvram" streams weights at ~half the peak, "novram" is the minimal-footprint last resort).
RENDER_TIERS: tuple[dict, ...] = (
    {"low_memory": "",        "width": 1024, "height": 1024, "steps": 30, "min_available": 8.0, "label": "full quality, 1024px"},
    {"low_memory": "lowvram", "width": 1024, "height": 1024, "steps": 28, "min_available": 5.5, "label": "1024px, low-memory mode"},
    {"low_memory": "lowvram", "width": 768,  "height": 768,  "steps": 24, "min_available": 3.5, "label": "768px, low-memory mode"},
    {"low_memory": "lowvram", "width": 512,  "height": 512,  "steps": 20, "min_available": 2.4, "label": "512px, low-memory mode"},
    {"low_memory": "novram",  "width": 512,  "height": 512,  "steps": 18, "min_available": 1.5, "label": "512px, minimal-memory mode"},
)
from core.ollama_endpoint import ollama_base_url as _ollama_base_url

_OLLAMA_BASE = _ollama_base_url().rstrip("/")
_COMFYUI_PORT = os.environ.get("VOOL_COMFYUI_PORT", "8188")


@dataclass
class GovernorDecision:
    ok: bool                              # does the task fit now (possibly after reclaim)?
    actions: list[str] = field(default_factory=list)   # human-readable reclaim steps taken
    footprint_gb: float = 0.0
    usable_before_gb: float = 0.0
    usable_after_gb: float = 0.0

    def note(self) -> str:
        if not self.actions:
            return ""
        return "Freed memory first: " + "; ".join(self.actions) + "."


# --------------------------------------------------------------------------- reclaim primitives

def unload_idle_ollama() -> float:
    """Ask Ollama to unload its loaded model(s) now (keep_alive=0). Returns GB freed (estimated).

    The model reloads automatically on the next chat turn (~seconds), so this is a cheap way to hand
    a render the RAM a prior chat is still holding idle. No-op if Ollama is unreachable or empty.
    """
    # Canonical LocalModelPolicy: a disabled runtime must not signal the local daemon at all —
    # a stray keep_alive=0 here would unload models belonging to some OTHER enabled process.
    from core.local_model_policy import local_models_enabled

    if not local_models_enabled():
        return 0.0
    loaded = sr._ollama_loaded_bytes()
    if loaded <= 0:
        return 0.0
    models: list[str] = []
    with contextlib.suppress(OSError, ValueError):
        req = urllib.request.Request(f"{_OLLAMA_BASE}/api/ps", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        models = [str(m.get("name") or m.get("model") or "") for m in (data.get("models") or [])]
    freed_any = False
    for name in [m for m in models if m]:
        payload = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
        with contextlib.suppress(OSError, ValueError):
            req = urllib.request.Request(
                f"{_OLLAMA_BASE}/api/generate", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=4.0).read()
            freed_any = True
    return (loaded / (1024.0 ** 3)) if freed_any else 0.0


def stop_comfyui() -> bool:
    """Gracefully stop an idle ComfyUI (SIGTERM the port-8188 process), releasing its ~5 GB.

    ensure_comfyui() restarts it on the next image request, so this is safe idle hygiene. Returns
    True if a process was signaled.
    """
    out = ""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(
            ["pgrep", "-f", f"main.py --port {_COMFYUI_PORT}"],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout or ""
    signaled = False
    for pid in [p for p in out.split() if p.strip().isdigit()]:
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(pid), signal.SIGTERM)
            signaled = True
    return signaled


# --------------------------------------------------------------------------- the gate

def ensure_headroom_for(footprint_gb: float, *, allow_stop_comfyui: bool = False) -> GovernorDecision:
    """Make room for a heavy task, or report that it will not fit. Auto-manages; never freezes.

    ``allow_stop_comfyui`` should be False when the task IS a render (we are about to use ComfyUI);
    True when reclaiming for something else (a big model load).
    """
    snap = sr.snapshot(force=True)
    decision = GovernorDecision(
        ok=snap.usable_gb >= footprint_gb, footprint_gb=footprint_gb,
        usable_before_gb=snap.usable_gb, usable_after_gb=snap.usable_gb,
    )
    if decision.ok:
        return decision

    # Reclaim 1: unload Ollama's idle model (reloads on the next chat turn).
    if snap.ollama_loaded_gb > 0.5:
        freed = unload_idle_ollama()
        if freed > 0:
            decision.actions.append(f"unloaded the idle local model (~{freed:.1f} GB, reloads on your next chat)")
            snap = sr.snapshot(force=True)

    # Reclaim 2: stop an idle ComfyUI, if this task is not itself a render.
    if snap.usable_gb < footprint_gb and allow_stop_comfyui and snap.comfyui_running and stop_comfyui():
        decision.actions.append("stopped the idle image engine (~5 GB)")
        snap = sr.snapshot(force=True)

    decision.usable_after_gb = snap.usable_gb
    decision.ok = snap.usable_gb >= footprint_gb
    return decision


def plan_render() -> tuple[dict | None, GovernorDecision]:
    """Reclaim idle memory, then pick the best render tier that fits the machine right now.

    Returns (tier, decision). ``tier`` is the richest RENDER_TIERS entry whose ``min_usable`` fits the
    current headroom — full 1024px when there is room, or a smaller low-memory render when tight — so an
    image ALWAYS renders instead of being refused. ``tier`` is None only when the Mac is too starved even
    for the 512px low-memory tier (rare, and only after reclaim); the caller then offers the cloud.
    """
    snap = sr.snapshot(force=True)
    actions: list[str] = []
    # Earn a better (larger/faster) tier by handing back idle memory — the model reloads on the next chat.
    if snap.available_ram_gb < RENDER_TIERS[0]["min_available"] and snap.ollama_loaded_gb > 0.5:
        freed = unload_idle_ollama()
        if freed > 0:
            actions.append(f"freed ~{freed:.1f} GB (unloaded the idle local model; it reloads on your next chat)")
            snap = sr.snapshot(force=True)
    # Gate on REAL free RAM (available), not available-minus-reserve — a user-requested render is a
    # transient foreground task and may use the ambient reserve while it runs.
    tier = next((t for t in RENDER_TIERS if snap.available_ram_gb >= t["min_available"]), None)
    lightest = RENDER_TIERS[-1]["min_available"]
    return tier, GovernorDecision(
        ok=tier is not None, actions=actions,
        footprint_gb=float(tier["min_available"]) if tier else float(lightest),
        usable_before_gb=snap.available_ram_gb, usable_after_gb=snap.available_ram_gb,
    )


def reclaim_if_pressured() -> list[str]:
    """Opportunistic idle hygiene: when memory is tight, hand back what VOOL is holding idle.

    Called after a heavy task finishes. Under warn/critical pressure (or low usable headroom), stop
    the now-idle ComfyUI so its ~5 GB does not sit resident and push the machine toward swap.
    """
    snap = sr.snapshot(force=True)
    freed: list[str] = []
    if (snap.memory_pressure in {"warn", "critical"} or snap.usable_gb < RENDER_FOOTPRINT_GB) and \
            snap.comfyui_running and stop_comfyui():
        freed.append("stopped the idle image engine (~5 GB)")
    return freed


# --------------------------------------------------------------- Phase 3: model-load governance
# The freeze pattern this closes: a big model (loaded by VOOL or ANY other Ollama client) sits
# resident on a small box, every lane starves, the fans roar, and nothing in the product can even
# SAY why. Phase 3 adds: see it (resident_models / resource_hog_report), gate it (plan_model_load
# before a call that would load a non-resident model), and fix it (reclaim_model / free_up_memory).

_LOAD_SAFETY_FLOOR_GB = float(os.environ.get("VOOL_MODEL_LOAD_FLOOR_GB", "1.5") or 1.5)
_RESIDENCY_CERTIFICATE_TTL_SECONDS = max(
    1.0,
    min(float(os.environ.get("VOOL_RESIDENCY_CERTIFICATE_TTL_SECONDS", "120") or 120), 900.0),
)
_RESIDENCY_EVIDENCE_SOURCES = frozenset(
    {
        "successful_ollama_model_call",
        "successful_ollama_prewarm",
        "successful_ollama_stream",
    }
)


@dataclass(frozen=True)
class ResidencyCertificate:
    """Short-lived evidence that this runtime actually used one exact local model."""

    provider_id: str
    model_tag: str
    model_digest: str
    ollama_endpoint: str
    ollama_process: dict[str, object]
    runtime_identity: str
    issued_at: float
    expires_at: float
    evidence_source: str
    llama_server_process: dict[str, object] | None = None


_RESIDENCY_CERTIFICATE: ResidencyCertificate | None = None


def _normalize_ollama_endpoint(endpoint: str | None) -> str:
    parsed = urlparse(str(endpoint or "").strip().rstrip("/"))
    if not parsed.scheme or not parsed.hostname:
        return ""
    path = str(parsed.path or "").rstrip("/")
    if path == "/v1":
        path = ""
    host = str(parsed.hostname).lower()
    port = parsed.port
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}{path}"


def _process_identity(pid: int | None) -> dict[str, object] | None:
    """Return a live PID plus start identity without making process control changes."""

    if pid is None:
        return None
    try:
        import psutil

        process = psutil.Process(int(pid))
        if not process.is_running():
            return None
        identity: dict[str, object] = {"pid": int(process.pid)}
        with contextlib.suppress(psutil.Error, OSError, ValueError):
            identity["create_time"] = float(process.create_time())
        return identity
    except (ImportError, OSError, ValueError):
        return None


def _ollama_process_identity(endpoint: str | None = None) -> dict[str, object] | None:
    """Find the loopback listener for an Ollama endpoint and bind its process instance."""

    parsed = urlparse(str(endpoint or _OLLAMA_BASE).strip())
    port = parsed.port or 11434
    try:
        import psutil

        candidates: list[int] = []
        for connection in psutil.net_connections(kind="tcp"):
            if str(getattr(connection, "status", "")).upper() != "LISTEN":
                continue
            address = getattr(connection, "laddr", None)
            if not address:
                continue
            try:
                address_port = int(address.port)
            except AttributeError:
                address_port = int(address[1]) if len(address) > 1 else 0
            if address_port != port:
                continue
            candidate_pid = getattr(connection, "pid", None)
            if candidate_pid is not None:
                candidates.append(int(candidate_pid))
        for candidate_pid in sorted(set(candidates)):
            identity = _process_identity(candidate_pid)
            if identity is not None:
                return identity
    except (ImportError, OSError, TypeError, ValueError, IndexError):
        pass
    return None


def _owned_llama_server_identity(ollama_pid: int | None) -> dict[str, object] | None:
    """Return an owned llama-server process identity when Ollama exposes one."""

    if ollama_pid is None:
        return None
    try:
        import psutil

        parent = psutil.Process(int(ollama_pid))
        children = parent.children(recursive=True)
        for child in sorted(children, key=lambda item: int(item.pid)):
            with contextlib.suppress(psutil.Error, OSError, ValueError):
                command = " ".join([str(child.name() or ""), *[str(item) for item in child.cmdline()]])
                executable = str(child.exe() or "")
                if "llama-server" not in f"{command} {executable}".lower():
                    continue
                return _process_identity(int(child.pid))
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return None


def current_runtime_identity() -> str:
    """Build a non-secret identity for the current diagnostic/API runtime instance."""

    explicit = str(os.environ.get("VOOL_RUNTIME_IDENTITY") or "").strip()
    if explicit:
        return explicit
    identity: dict[str, object] = {"pid": os.getpid()}
    process = _process_identity(os.getpid())
    if process and "create_time" in process:
        identity["create_time"] = process["create_time"]
    manifest_path = str(os.environ.get("VOOL_BUNDLE_MANIFEST") or "").strip()
    if manifest_path:
        with contextlib.suppress(OSError):
            with open(manifest_path, "rb") as manifest_file:
                identity["bundle_manifest_sha256"] = hashlib.sha256(manifest_file.read()).hexdigest()
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def current_model_digest(model_name: str, endpoint: str | None = None) -> str:
    """Read the exact installed digest for one tag from the current Ollama instance."""

    normalized_endpoint = _normalize_ollama_endpoint(endpoint or _OLLAMA_BASE)
    if not normalized_endpoint:
        return ""
    with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
        request = urllib.request.Request(
            f"{normalized_endpoint}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        wanted = str(model_name or "").strip()
        for row in payload.get("models") or []:
            if not isinstance(row, dict):
                continue
            names = {str(row.get("name") or "").strip(), str(row.get("model") or "").strip()}
            digest = str(row.get("digest") or "").strip()
            if wanted in names and digest:
                return digest
    return ""


def clear_residency_certificate() -> None:
    """Clear the process-local certificate; intended for shutdown and deterministic tests."""

    global _RESIDENCY_CERTIFICATE
    _RESIDENCY_CERTIFICATE = None


def record_residency_evidence(
    *,
    provider_id: str,
    model_tag: str,
    model_digest: str,
    endpoint: str,
    evidence_source: str,
    runtime_identity: str | None = None,
    now: float | None = None,
    ttl_seconds: float | None = None,
    ollama_process: dict[str, object] | None = None,
    llama_server_process: dict[str, object] | None = None,
) -> bool:
    """Issue a bounded certificate only for a successful, exact-model Ollama operation."""

    global _RESIDENCY_CERTIFICATE
    provider = str(provider_id or "").strip()
    model = str(model_tag or "").strip()
    digest = str(model_digest or "").strip()
    normalized_endpoint = _normalize_ollama_endpoint(endpoint)
    source = str(evidence_source or "").strip()
    if not provider or not model or not normalized_endpoint:
        return False
    if source not in _RESIDENCY_EVIDENCE_SOURCES:
        return False
    observed_process = ollama_process or _ollama_process_identity(normalized_endpoint)
    if not observed_process or not observed_process.get("pid"):
        return False
    if not digest:
        digest = current_model_digest(model, normalized_endpoint)
    if not digest or digest.lower() in {"unknown", "none", "null"}:
        return False
    runtime = str(runtime_identity or current_runtime_identity()).strip()
    if not runtime:
        return False
    issued_at = float(time.time() if now is None else now)
    ttl = (
        _RESIDENCY_CERTIFICATE_TTL_SECONDS
        if ttl_seconds is None
        else max(1.0, min(float(ttl_seconds), 900.0))
    )
    _RESIDENCY_CERTIFICATE = ResidencyCertificate(
        provider_id=provider,
        model_tag=model,
        model_digest=digest,
        ollama_endpoint=normalized_endpoint,
        ollama_process=dict(observed_process),
        runtime_identity=runtime,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        evidence_source=source,
        llama_server_process=dict(
            llama_server_process
            or _owned_llama_server_identity(int(observed_process["pid"]))
            or {}
        ) or None,
    )
    return True


def _same_process_instance(expected: dict[str, object], current: dict[str, object] | None) -> bool:
    if not current or int(current.get("pid") or 0) != int(expected.get("pid") or 0):
        return False
    expected_start = expected.get("create_time")
    current_start = current.get("create_time")
    if expected_start is not None:
        if current_start is None:
            return False
        return abs(float(expected_start) - float(current_start)) <= 1e-6
    return True


def _certificate_matches(
    *,
    provider_id: str,
    model_tag: str,
    model_digest: str,
    endpoint: str,
    runtime_identity: str | None,
) -> bool:
    certificate = _RESIDENCY_CERTIFICATE
    if certificate is None or float(time.time()) >= certificate.expires_at:
        return False
    if certificate.provider_id != str(provider_id or "").strip():
        return False
    if certificate.model_tag != str(model_tag or "").strip():
        return False
    expected_digest = str(model_digest or "").strip()
    if expected_digest and certificate.model_digest != expected_digest:
        return False
    normalized_endpoint = _normalize_ollama_endpoint(endpoint)
    if not normalized_endpoint or certificate.ollama_endpoint != normalized_endpoint:
        return False
    expected_runtime = str(runtime_identity or current_runtime_identity()).strip()
    if not expected_runtime or certificate.runtime_identity != expected_runtime:
        return False
    current_process = _ollama_process_identity(certificate.ollama_endpoint)
    if not _same_process_instance(certificate.ollama_process, current_process):
        return False
    if certificate.llama_server_process:
        current_runner = _owned_llama_server_identity(int(certificate.ollama_process["pid"]))
        if not _same_process_instance(certificate.llama_server_process, current_runner):
            return False
    if not expected_digest:
        current_digest = current_model_digest(certificate.model_tag, certificate.ollama_endpoint)
        if not current_digest or current_digest != certificate.model_digest:
            return False
    return True


def resident_models() -> list[dict]:
    """What Ollama is holding in memory right now, including digest when reported."""
    from core.local_model_policy import local_models_enabled

    if not local_models_enabled():
        return []
    rows: list[dict] = []
    with contextlib.suppress(OSError, ValueError):
        req = urllib.request.Request(f"{_OLLAMA_BASE}/api/ps", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        for m in data.get("models") or []:
            name = str(m.get("name") or m.get("model") or "").strip()
            size = float(m.get("size_vram") or m.get("size") or 0) / (1024.0 ** 3)
            if name:
                rows.append(
                    {
                        "name": name,
                        "size_gb": round(size, 1),
                        "digest": str(m.get("digest") or "").strip(),
                    }
                )
    rows.sort(key=lambda r: r["size_gb"], reverse=True)
    return rows


def estimated_model_gb(model_name: str) -> float:
    """Expected in-memory footprint for a model, 0.0 when unknown (unknown never gates)."""
    try:
        from core.local_model_bundles import model_storage_gb

        known = float(model_storage_gb(model_name) or 0.0)
    except Exception:
        known = 0.0
    if known > 0:
        return known
    from core.local_model_policy import local_models_enabled

    if not local_models_enabled():
        return 0.0  # no discovery probe under a disabled policy; unknown never gates
    with contextlib.suppress(OSError, ValueError):  # fall back to the installed-tag disk size
        req = urllib.request.Request(f"{_OLLAMA_BASE}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        for m in data.get("models") or []:
            if str(m.get("name") or "").strip() == str(model_name).strip():
                return round(float(m.get("size") or 0) / (1024.0 ** 3), 1)
    return 0.0


def _unload_models(names: list[str]) -> list[str]:
    """Ask Ollama to unload specific models (keep_alive=0). Returns the names that were signaled."""
    from core.local_model_policy import local_models_enabled

    if not local_models_enabled():
        return []
    done: list[str] = []
    for name in [n for n in names if n]:
        payload = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
        with contextlib.suppress(OSError, ValueError):
            req = urllib.request.Request(
                f"{_OLLAMA_BASE}/api/generate", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=4.0).read()
            done.append(name)
    return done


def reclaim_model(model_name: str) -> bool:
    """Unload ONE named model from Ollama (it reloads on its next use). True if signaled."""
    return bool(_unload_models([str(model_name or "").strip()]))


def plan_model_load(
    model_name: str,
    *,
    provider_id: str = "",
    model_digest: str = "",
    endpoint: str | None = None,
    runtime_identity: str | None = None,
) -> GovernorDecision:
    """Gate a chat-model load the way renders are gated: fit, reclaim, or refuse — never a freeze.

    * Already resident -> ok (serving a loaded model costs no new RAM; never gate it).
    * Unknown size -> ok (never block on ignorance).
    * Fits real free RAM (+ a small floor) -> ok.
    * Else: unload OTHER resident models (they reload on their next use), re-check.
    * Still no room -> ok=False; the caller degrades (smaller model / cloud lane) instead of
      swap-thrashing the machine.
    """
    name = str(model_name or "").strip()
    snap = sr.snapshot(force=True)
    residents = resident_models()
    expected_digest = str(model_digest or "").strip()
    if any(
        str(r.get("name") or "") == name
        and (
            (bool(expected_digest) and str(r.get("digest") or "") == expected_digest)
            or (not expected_digest and not provider_id)
            or (not expected_digest and provider_id and bool(str(r.get("digest") or "").strip()))
        )
        for r in residents
    ):
        return GovernorDecision(ok=True, usable_before_gb=snap.available_ram_gb, usable_after_gb=snap.available_ram_gb)
    if not residents and provider_id and _certificate_matches(
        provider_id=provider_id,
        model_tag=name,
        model_digest=expected_digest,
        endpoint=endpoint or _OLLAMA_BASE,
        runtime_identity=runtime_identity,
    ):
        return GovernorDecision(ok=True, usable_before_gb=snap.available_ram_gb, usable_after_gb=snap.available_ram_gb)
    need = estimated_model_gb(name)
    decision = GovernorDecision(
        ok=True, footprint_gb=need,
        usable_before_gb=snap.available_ram_gb, usable_after_gb=snap.available_ram_gb,
    )
    if need <= 0:
        return decision
    if snap.available_ram_gb >= need + _LOAD_SAFETY_FLOOR_GB:
        return decision
    others = [r["name"] for r in residents if r["name"] != name]
    if others:
        freed = _unload_models(others)
        if freed:
            decision.actions.append(
                "unloaded " + ", ".join(freed) + " to make room (each reloads on its next use)"
            )
            snap = sr.snapshot(force=True)
    decision.usable_after_gb = snap.available_ram_gb
    decision.ok = snap.available_ram_gb >= need + _LOAD_SAFETY_FLOOR_GB
    return decision


def resource_hog_report() -> dict:
    """A plain answer to "why is my Mac slow/loud?": pressure, swap, and who is eating the RAM."""
    snap = sr.snapshot(force=True)
    residents = resident_models()
    known_names = set()
    try:
        from core.local_model_bundles import MODEL_STORAGE_GB

        known_names = set(MODEL_STORAGE_GB)
    except Exception:
        pass
    hog = residents[0] if residents else None
    return {
        "total_gb": round(snap.total_ram_gb, 1),
        "available_gb": round(snap.available_ram_gb, 1),
        "pressure": snap.memory_pressure,
        "swap_used_gb": round(snap.swap_used_gb, 1),
        "load_per_core": round(snap.load_per_core, 2),
        "comfyui_running": snap.comfyui_running,
        "resident_models": residents,
        "hog": hog,
        "hog_is_vool_routable": bool(hog and hog["name"] in known_names),
        "squeezed": bool(
            snap.memory_pressure in {"warn", "critical"}
            or snap.swap_used_gb > 4.0
            or (hog and hog["size_gb"] > snap.total_ram_gb * 0.5)
        ),
    }


def free_up_memory() -> dict:
    """Explicit owner ask ("free up memory"): unload EVERY resident model + stop an idle ComfyUI.

    Models reload on their next use; ComfyUI restarts on the next image request. Returns
    before/after numbers so the reply can show exactly what changed.
    """
    before = sr.snapshot(force=True)
    residents = resident_models()
    unloaded = _unload_models([r["name"] for r in residents])
    stopped = stop_comfyui() if before.comfyui_running else False
    after = sr.snapshot(force=True)
    return {
        "unloaded": unloaded,
        "stopped_comfyui": stopped,
        "available_before_gb": round(before.available_ram_gb, 1),
        "available_after_gb": round(after.available_ram_gb, 1),
        "freed_gb": round(max(0.0, after.available_ram_gb - before.available_ram_gb), 1),
    }


__all__ = [
    "RENDER_FOOTPRINT_GB",
    "RENDER_TIERS",
    "GovernorDecision",
    "ResidencyCertificate",
    "clear_residency_certificate",
    "current_model_digest",
    "current_runtime_identity",
    "ensure_headroom_for",
    "estimated_model_gb",
    "free_up_memory",
    "plan_model_load",
    "plan_render",
    "reclaim_if_pressured",
    "reclaim_model",
    "record_residency_evidence",
    "resident_models",
    "resource_hog_report",
    "stop_comfyui",
    "unload_idle_ollama",
]
