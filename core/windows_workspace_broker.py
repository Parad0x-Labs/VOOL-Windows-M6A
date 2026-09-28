"""The narrow Windows native boundary for one exact workspace file replacement.

This module only builds and transports ``workspace_write_v1`` requests. The native broker owns
the AppContainer/Job Object and the filesystem mutation; the API process never writes the target
on Windows. Request content travels over broker stdin and is never placed in command-line args.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from core.plugin_executor import _windows_broker_environment, _windows_broker_path

SCHEMA = "vool.workspace_write.v1"
EFFECT = "workspace_write_v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
CONFINEMENT_EVIDENCE_SCHEMA = "vool.workspace_write_confinement_evidence_v1"


_MISSING = object()


def _native_field(value: Any, name: str) -> Any:
    """Read a native broker field from either its wire or test-double spelling."""

    if not isinstance(value, dict):
        return _MISSING
    if name in value:
        return value[name]
    pascal = "".join(part[:1].upper() + part[1:] for part in name.split("_"))
    return value.get(pascal, _MISSING)


def _required_text(value: Any, name: str) -> str | None:
    field = _native_field(value, name)
    if field is _MISSING or not isinstance(field, str) or not field.strip():
        return None
    return field.strip()


def _required_bool(value: Any, name: str) -> bool | None:
    field = _native_field(value, name)
    if field is _MISSING or not isinstance(field, bool):
        return None
    return field


def _required_int(value: Any, name: str) -> int | None:
    field = _native_field(value, name)
    if field is _MISSING or isinstance(field, bool):
        return None
    try:
        return int(field)
    except (TypeError, ValueError):
        return None


def _project_token_evidence(token: Any) -> dict[str, Any] | None:
    if not isinstance(token, dict):
        return None
    is_app_container = _required_bool(token, "is_app_container")
    has_restrictions = _required_bool(token, "has_restrictions")
    integrity_level = _required_text(token, "integrity_level")
    integrity_level_rid = _required_int(token, "integrity_level_rid")
    restricted_sid_count = _required_int(token, "restricted_sid_count")
    capabilities = _native_field(token, "capabilities")
    if (
        is_app_container is None
        or has_restrictions is None
        or integrity_level is None
        or integrity_level_rid is None
        or restricted_sid_count is None
        or not isinstance(capabilities, list)
    ):
        return None
    safe_capabilities = [item.strip() for item in capabilities if isinstance(item, str) and item.strip()]
    if len(safe_capabilities) != len(capabilities):
        return None
    return {
        "is_app_container": is_app_container,
        "has_restrictions": has_restrictions,
        "integrity_level_rid": integrity_level_rid,
        "integrity_level": integrity_level,
        "restricted_sid_count": restricted_sid_count,
        "capabilities": safe_capabilities,
    }


def _project_job_evidence(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    names = (
        "limit_flags",
        "active_process_limit",
        "process_memory_limit_bytes",
        "job_memory_limit_bytes",
        "per_process_user_time_limit_100_ns",
        "cpu_control_flags",
        "cpu_rate",
    )
    values = {name: _required_int(job, name) for name in names}
    if any(value is None for value in values.values()):
        return None
    return {name: int(value) for name, value in values.items()}


def _project_filesystem_grants(grants: Any) -> list[dict[str, Any]] | None:
    if not isinstance(grants, list) or not grants:
        return None
    projected: list[dict[str, Any]] = []
    for grant in grants:
        if not isinstance(grant, dict):
            return None
        access = _required_text(grant, "access")
        rights = _required_int(grant, "rights")
        inheritance = _required_text(grant, "inheritance")
        propagation = _required_text(grant, "propagation")
        if access is None or rights is None or inheritance is None or propagation is None:
            return None
        # The native receipt's path is intentionally omitted. The action record already binds the
        # exact relative target; persisting staging/worker directory paths adds machine-local state
        # without improving the durable confinement claim.
        projected.append(
            {
                "access": access,
                "rights": rights,
                "inheritance": inheritance,
                "propagation": propagation,
            }
        )
    return projected


def project_workspace_write_confinement_evidence(
    broker_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the native receipt into the durable, non-secret workspace-write evidence schema.

    The native broker owns the full receipt. Durable runtime records receive only fields needed to
    prove the exact operation's confinement and result. No environment, approval material, raw ACL,
    workspace root, target path, executable path, argv, or grant path is persisted here.
    """

    if not isinstance(broker_result, dict):
        return {}
    confinement = broker_result.get("confinement")
    if not isinstance(confinement, dict):
        return {}

    text_fields = (
        "platform",
        "broker_type",
        "effect",
        "tool",
        "operation_id",
        "relative_target",
        "prior_state",
        "network_policy",
        "result",
        "appcontainer_name",
        "appcontainer_sid",
        "content_sha256",
    )
    text_values = {name: _required_text(confinement, name) for name in text_fields}
    if any(value is None for value in text_values.values()):
        return {}
    prior_sha256 = _native_field(confinement, "prior_sha256")
    if prior_sha256 is _MISSING or not isinstance(prior_sha256, str):
        return {}

    bool_fields = ("job_owned", "verified", "timed_out")
    bool_values = {name: _required_bool(confinement, name) for name in bool_fields}
    if any(value is None for value in bool_values.values()):
        return {}

    int_fields = ("process_id", "content_length", "timeout_ms", "exit_code")
    int_values = {name: _required_int(confinement, name) for name in int_fields}
    if any(value is None for value in int_values.values()):
        return {}

    token = _project_token_evidence(_native_field(confinement, "token"))
    job = _project_job_evidence(_native_field(confinement, "job"))
    filesystem_grants = _project_filesystem_grants(_native_field(confinement, "filesystem_grants"))
    if token is None or job is None or filesystem_grants is None:
        return {}

    final_sha256 = _native_field(broker_result, "final_sha256")
    atomic_installed = _required_bool(broker_result, "atomic_installed")
    broker_ok = _required_bool(broker_result, "ok")
    if (
        not isinstance(final_sha256, str)
        or (broker_ok is True and not final_sha256.strip())
        or atomic_installed is None
        or broker_ok is None
    ):
        return {}

    return {
        "schema": CONFINEMENT_EVIDENCE_SCHEMA,
        "platform": text_values["platform"],
        "broker_type": text_values["broker_type"],
        "effect": text_values["effect"],
        "tool": text_values["tool"],
        "operation_id": text_values["operation_id"],
        "relative_target": text_values["relative_target"],
        "prior_state": text_values["prior_state"],
        "prior_sha256": prior_sha256.strip(),
        "content_sha256": text_values["content_sha256"],
        "content_length": int(int_values["content_length"]),
        "appcontainer_name": text_values["appcontainer_name"],
        "appcontainer_sid": text_values["appcontainer_sid"],
        "process_id": int(int_values["process_id"]),
        "job_owned": bool(bool_values["job_owned"]),
        "filesystem_grants": filesystem_grants,
        "network_policy": text_values["network_policy"],
        "timeout_ms": int(int_values["timeout_ms"]),
        "result": text_values["result"],
        "verified": bool(bool_values["verified"]),
        "exit_code": int(int_values["exit_code"]),
        "timed_out": bool(bool_values["timed_out"]),
        "token_security": token,
        "job_security": job,
        "final_sha256": final_sha256.strip(),
        "atomic_installed": bool(atomic_installed),
        "broker_ok": bool(broker_ok),
    }


def sanitize_workspace_write_confinement_evidence(
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and copy an already-projected evidence object for durable storage."""

    if not isinstance(evidence, dict) or evidence.get("schema") != CONFINEMENT_EVIDENCE_SCHEMA:
        return {}
    string_fields = (
        "schema",
        "platform",
        "broker_type",
        "effect",
        "tool",
        "operation_id",
        "relative_target",
        "prior_state",
        "prior_sha256",
        "content_sha256",
        "appcontainer_name",
        "appcontainer_sid",
        "network_policy",
        "result",
        "final_sha256",
    )
    if any(not isinstance(evidence.get(name), str) for name in string_fields):
        return {}
    bool_fields = ("job_owned", "verified", "timed_out", "atomic_installed", "broker_ok")
    if any(not isinstance(evidence.get(name), bool) for name in bool_fields):
        return {}
    int_fields = ("content_length", "process_id", "timeout_ms", "exit_code")
    if any(isinstance(evidence.get(name), bool) or not isinstance(evidence.get(name), int) for name in int_fields):
        return {}

    raw_grants = evidence.get("filesystem_grants")
    if not isinstance(raw_grants, list) or not raw_grants:
        return {}
    grants: list[dict[str, Any]] = []
    for grant in raw_grants:
        if not isinstance(grant, dict):
            return {}
        if (
            not isinstance(grant.get("access"), str)
            or isinstance(grant.get("rights"), bool)
            or not isinstance(grant.get("rights"), int)
            or not isinstance(grant.get("inheritance"), str)
            or not isinstance(grant.get("propagation"), str)
        ):
            return {}
        grants.append(
            {
                "access": grant["access"],
                "rights": int(grant["rights"]),
                "inheritance": grant["inheritance"],
                "propagation": grant["propagation"],
            }
        )

    raw_token = evidence.get("token_security")
    if not isinstance(raw_token, dict):
        return {}
    token = {
        "is_app_container": raw_token.get("is_app_container"),
        "has_restrictions": raw_token.get("has_restrictions"),
        "integrity_level_rid": raw_token.get("integrity_level_rid"),
        "integrity_level": raw_token.get("integrity_level"),
        "restricted_sid_count": raw_token.get("restricted_sid_count"),
        "capabilities": raw_token.get("capabilities"),
    }
    if (
        not isinstance(token["is_app_container"], bool)
        or not isinstance(token["has_restrictions"], bool)
        or isinstance(token["integrity_level_rid"], bool)
        or not isinstance(token["integrity_level_rid"], int)
        or not isinstance(token["integrity_level"], str)
        or isinstance(token["restricted_sid_count"], bool)
        or not isinstance(token["restricted_sid_count"], int)
        or not isinstance(token["capabilities"], list)
        or any(not isinstance(item, str) for item in token["capabilities"])
    ):
        return {}

    raw_job = evidence.get("job_security")
    if not isinstance(raw_job, dict):
        return {}
    job_fields = (
        "limit_flags",
        "active_process_limit",
        "process_memory_limit_bytes",
        "job_memory_limit_bytes",
        "per_process_user_time_limit_100_ns",
        "cpu_control_flags",
        "cpu_rate",
    )
    if any(isinstance(raw_job.get(name), bool) or not isinstance(raw_job.get(name), int) for name in job_fields):
        return {}
    job = {name: int(raw_job[name]) for name in job_fields}

    return {
        **{name: evidence[name] for name in string_fields},
        "content_length": int(evidence["content_length"]),
        "process_id": int(evidence["process_id"]),
        "timeout_ms": int(evidence["timeout_ms"]),
        "exit_code": int(evidence["exit_code"]),
        **{name: evidence[name] for name in bool_fields},
        "filesystem_grants": grants,
        "token_security": token,
        "job_security": job,
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _runtime_identity(source_context: dict[str, Any] | None, *, session_id: str) -> str:
    context = dict(source_context or {})
    execution = context.get("_execution_identity")
    execution = execution if isinstance(execution, dict) else {}
    parts = [
        str(execution.get("request_id") or context.get("request_id") or "").strip(),
        str(execution.get("execution_id") or "").strip(),
        str(execution.get("attempt_id") or "").strip(),
        str(execution.get("generation") or "").strip(),
    ]
    identity = "|".join(part for part in parts if part)
    return identity or f"session:{session_id or 'direct'}"


def compute_effect_identity(
    *,
    tool: str,
    approval_id: str,
    proposal_id: str,
    operation_id: str,
    runtime_id: str,
    workspace_root: str,
    relative_target: str,
    prior_state: str,
    prior_sha256: str,
    content_sha256: str,
    content_length: int,
) -> str:
    canonical = "\n".join(
        (
            SCHEMA,
            EFFECT,
            tool,
            approval_id,
            proposal_id,
            operation_id,
            runtime_id,
            workspace_root,
            relative_target,
            prior_state,
            prior_sha256,
            content_sha256,
            str(int(content_length)),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_workspace_write_request(
    *,
    tool: str = "workspace.write_file",
    target: Path,
    workspace_root: Path,
    content: str,
    source_context: dict[str, Any] | None,
    session_id: str,
    approval_id: str = "",
    approval_status: str = "",
    proposal_id: str = "",
    prior_state: str | None = None,
    prior_sha256: str | None = None,
) -> dict[str, Any]:
    root = workspace_root.resolve()
    canonical_target = target.resolve(strict=False)
    relative_target = canonical_target.relative_to(root).as_posix()
    content_bytes = content.encode("utf-8")
    if prior_state is None:
        if canonical_target.exists():
            if not canonical_target.is_file() or canonical_target.is_symlink():
                raise ValueError("workspace write target is not a regular file")
            prior_state = "file"
            prior_sha256 = _sha256_bytes(canonical_target.read_bytes())
        else:
            prior_state = "absent"
            prior_sha256 = ""
    else:
        prior_state = str(prior_state)
        prior_sha256 = str(prior_sha256 or "")

    clean_approval = str(approval_id or "").strip()
    clean_approval_status = str(approval_status or "").strip() or (
        "policy_allowed" if not clean_approval else "consumed"
    )
    if not clean_approval:
        clean_approval = f"policy-{uuid.uuid4().hex}"
    clean_proposal = str(proposal_id or "").strip() or clean_approval
    # A one-shot mode approval is itself the stable proposal/operation binding. Standing policy
    # callers get a unique operation id, while the policy decision remains the authority.
    operation_id = clean_approval if not clean_approval.startswith("policy-") else f"op-{uuid.uuid4().hex}"
    runtime_id = _runtime_identity(source_context, session_id=session_id)
    content_sha256 = _sha256_bytes(content_bytes)
    effect_identity = compute_effect_identity(
        tool=tool,
        approval_id=clean_approval,
        proposal_id=clean_proposal,
        operation_id=operation_id,
        runtime_id=runtime_id,
        workspace_root=str(root),
        relative_target=relative_target,
        prior_state=prior_state,
        prior_sha256=prior_sha256,
        content_sha256=content_sha256,
        content_length=len(content_bytes),
    )
    return {
        "schema": SCHEMA,
        "effect": EFFECT,
        "tool": tool,
        "approval_id": clean_approval,
        "approval_status": clean_approval_status,
        "proposal_id": clean_proposal,
        "operation_id": operation_id,
        "runtime_id": runtime_id,
        "workspace_root": str(root),
        "relative_target": relative_target,
        "target_path": str(canonical_target),
        "prior_state": prior_state,
        "prior_sha256": prior_sha256,
        "content_sha256": content_sha256,
        "content_length": len(content_bytes),
        "content_base64": base64.b64encode(content_bytes).decode("ascii"),
        "approved_effect_identity": effect_identity,
        "timeout_ms": 30_000,
    }


def run_workspace_write_broker(
    *,
    tool: str = "workspace.write_file",
    target: Path,
    workspace_root: Path,
    content: str,
    source_context: dict[str, Any] | None,
    session_id: str,
    approval_id: str = "",
    approval_status: str = "",
    proposal_id: str = "",
    prior_state: str | None = None,
    prior_sha256: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    broker = _windows_broker_path()
    if broker is None:
        return {
            "ok": False,
            "status": "confinement_unavailable",
            "error": "Native Windows workspace broker executable was not found; the target was not written.",
        }

    try:
        payload = build_workspace_write_request(
            tool=tool,
            target=target,
            workspace_root=workspace_root,
            content=content,
            source_context=source_context,
            session_id=session_id,
            approval_id=approval_id,
            approval_status=approval_status,
            proposal_id=proposal_id,
            prior_state=prior_state,
            prior_sha256=prior_sha256,
        )
        completed = subprocess.run(
            [str(broker), "workspace"],
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout_seconds) + 10.0),
            cwd=str(workspace_root.resolve()),
            env={
                **_windows_broker_environment(),
                "TEMP": str(target.resolve(strict=False).parent),
                "TMP": str(target.resolve(strict=False).parent),
            },
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timeout",
            "error": "Native Windows workspace broker exceeded its bounded timeout; the target was not confirmed.",
        }
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "status": "confinement_unavailable", "error": str(exc)}

    stdout = (completed.stdout or "").strip()
    try:
        result = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "confinement_unavailable",
            "error": "Native Windows workspace broker did not emit JSON.",
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "status": "confinement_unavailable",
            "error": "Native Windows workspace broker emitted a non-object result.",
        }
    if not result.get("ok") and not result.get("error"):
        result["error"] = (completed.stderr or "").strip()[:500]
    return result


__all__ = [
    "CONFINEMENT_EVIDENCE_SCHEMA",
    "EFFECT",
    "SCHEMA",
    "build_workspace_write_request",
    "compute_effect_identity",
    "project_workspace_write_confinement_evidence",
    "run_workspace_write_broker",
    "sanitize_workspace_write_confinement_evidence",
]
