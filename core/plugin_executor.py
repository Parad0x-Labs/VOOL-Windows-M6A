"""Run a plugin's tool as a subprocess: argument checked, environment withheld, process confined.

Wire protocol `json_stdio_v1`: one JSON object in on stdin, one JSON object out on stdout.

    in   {"intent": "...", "arguments": {...}, "context": {"scratch_dir": "...", "secrets": [...]}}
    out  {"ok": true,  "text": "...", "observation": {...}, "resolved_target": "..."}
     or  {"ok": false, "status": "invalid_arguments", "error": "..."}

An `ok:false` is fed back to the model as an observation, exactly like a built-in failure, so a
plugin can say "that project key does not exist" and the model can correct itself.

**Environment is an allowlist, not a filter.** `core/mcp_client.py` used to build a child
environment as `{**os.environ, **self.env}` — the whole environment, including whatever API keys
and tokens happen to be exported. This machine holds live keypairs, and a plugin is code a user
downloaded. Here a plugin receives only the variables its manifest names, plus the minimum needed
to start a process at all.

**Confinement is unconditional.** A minimal environment stops a child *inheriting* a secret; it
does nothing about a child *reading* one from disk, or writing wherever it likes. Every plugin
and MCP child therefore runs under the host's kernel confinement — the same Seatbelt (macOS) and
bwrap/unshare/firejail (Linux) wrappers `sandbox.job_runner` already uses for sandbox commands:
writes confined to the plugin's own root and its scratch directory, reads of the credential
directories and VOOL's key home denied, network denied unless the manifest or server config
says otherwise. A host that cannot enforce that REFUSES to run the child (status
``confinement_unavailable``) rather than silently running it bare; the only way past is the
explicit, informed ``confinement: "heuristic_only"`` opt-in, recorded on the result.

**Arguments are validated before dispatch, not after.** This is the pre-execution shape guard: a
value bound to a parameter declared `x-vool-kind: "name"` that parses as a filesystem path is
refused before the handler runs. `machine.find_folder` is the cautionary case — by the time an
answer exists to inspect, a whole-disk scan has already happened. Type-valid is not shape-valid.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import json_schema_lite

DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

# Without these a subprocess frequently cannot start or resolve anything. Deliberately minimal:
# no HOME, no SSH_AUTH_SOCK, no cloud credential variables, nothing a keypair could hide in.
_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")

# What a `x-vool-kind` means about a value's SHAPE, as opposed to its JSON type. A path and a bare
# name are both `"type": "string"`; only this separates them.
_PATH_LIKE_PREFIXES = ("~", "/", "./", "../")

CONFINEMENT_MODES = frozenset({"auto", "heuristic_only"})


class ConfinementUnavailableError(RuntimeError):
    """This host cannot confine a child process and the caller did not opt into running bare."""


@dataclass(frozen=True)
class PluginToolResult:
    ok: bool
    status: str = "executed"
    text: str = ""
    observation: dict[str, Any] = field(default_factory=dict)
    resolved_target: str = ""
    error: str = ""
    #: How the child was confined: ``kernel:<backend>`` or ``heuristic_only`` (explicit opt-in).
    #: Empty when the child never started.
    confinement: str = ""
    #: Non-secret evidence returned by an OS-enforced broker, when available.
    confinement_evidence: dict[str, Any] = field(default_factory=dict)

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"observation": dict(self.observation)}
        if self.resolved_target:
            details["resolved_target"] = self.resolved_target
        if self.error:
            details["error"] = self.error
        if self.confinement:
            details["confinement"] = self.confinement
        if self.confinement_evidence:
            details["confinement_evidence"] = dict(self.confinement_evidence)
        return details


def _looks_like_a_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.startswith(_PATH_LIKE_PREFIXES) or (os.sep in text and " " not in text.strip())


def check_argument_shapes(arguments: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Refuse values whose SHAPE contradicts what the parameter is declared to mean.

    Runs before the handler. A path arriving where a bare name was declared is the whole
    `machine.find_folder` failure — the value is a perfectly valid string, so schema validation
    passes it, and the cost is paid before anything can inspect the answer.
    """

    problems: list[str] = []
    properties = (schema or {}).get("properties")
    if not isinstance(properties, dict):
        return problems
    for name in properties:
        if name not in arguments:
            continue
        kind = str(json_schema_lite.annotations(schema, name).get("kind") or "")
        value = arguments[name]
        if not isinstance(value, str) or not kind:
            continue
        if kind == "name" and _looks_like_a_path(value):
            problems.append(
                f"{name}: expected a bare name but got what looks like a path ({value!r}). "
                "If you meant that location, pass it to a tool that takes a path."
            )
        elif kind == "path" and not _looks_like_a_path(value):
            problems.append(f"{name}: expected a path but got {value!r}")
        elif kind == "url" and "://" not in value:
            problems.append(f"{name}: expected a URL but got {value!r}")
    return problems


def build_child_env(
    *,
    env_allowlist: tuple[str, ...] = (),
    secrets: dict[str, str] | None = None,
    scratch_dir: str = "",
) -> dict[str, str]:
    """Exactly what the child may see. Everything not named here is withheld."""

    child: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            child[key] = value
    if os.name == "nt":
        system_root = str(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "").strip()
        child.setdefault(
            "PATH",
            os.pathsep.join(
                value for value in (
                    str(Path(sys.executable).resolve().parent),
                    str(Path(system_root) / "System32") if system_root else "",
                    system_root,
                ) if value
            ),
        )
    else:
        child.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    for key in env_allowlist:
        value = os.environ.get(str(key))
        if value:
            child[str(key)] = value
    for key, value in (secrets or {}).items():
        if str(key) and value:
            child[str(key)] = str(value)
    if scratch_dir:
        child["PLUGIN_SCRATCH"] = scratch_dir
    return child


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


def _program_read_roots(argv: list[str]) -> tuple[Path, ...]:
    """The program a confined child was told to run, so that child can read it.

    The Seatbelt profile denies reading the CONTENTS of the operator's home and names its
    exceptions explicitly. The interpreter's own prefixes are named for it; the script or binary
    the caller chose is not. An MCP server or plugin entrypoint living anywhere else under the
    home therefore died with "Operation not permitted" before it executed a line -- the child
    could not read its own program. Only paths that already resolve are named, and never a root
    as broad as the home directory itself, so this widens nothing beyond the argv the caller
    already chose to execute.
    """

    out: list[Path] = []
    seen: set[str] = set()
    try:
        home: Path | None = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for token in argv:
        text = str(token or "").strip()
        if not text or text.startswith("-"):
            continue
        try:
            candidate = Path(text)
            if not candidate.exists():
                located = shutil.which(text)
                if not located:
                    continue
                candidate = Path(located)
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if str(resolved) == resolved.anchor or (home is not None and resolved == home):
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return tuple(out)


def _kernel_confinement_prefix(
    argv: list[str],
    *,
    writable_roots: tuple[Path, ...],
    allow_network: bool = False,
    working_directory: Path | None = None,
) -> list[str] | None:
    """Wrap *argv* in the host's kernel confinement, or None when no backend can enforce it.

    Reuses `sandbox.job_runner`'s backends so plugins, MCP servers and sandbox commands share ONE
    confinement implementation (Seatbelt on macOS; bwrap, then unshare/firejail on Linux). Every
    backend is probed for whether it actually runs, not merely whether it is on PATH. Writes are
    confined to `writable_roots`; the credential directories and VOOL's key home are read-denied;
    network is denied unless `allow_network`.
    """

    from sandbox.job_runner import JobRunner
    from sandbox.resource_limits import ExecutionPolicy, default_read_roots

    roots = tuple(Path(p).resolve() for p in writable_roots if str(p))
    # READ-ONLY CONFINEMENT (the 2026-09-02 amendment's own intent): zero writable roots is a
    # VALID policy -- a child confined to write nowhere -- not "no backend". The former
    # `return None` made every read_only plugin child refuse with confinement_unavailable on
    # hosts that can confine, so the read-only claim was never mechanically enforced. The
    # Read the actual working directory as well as the program: a handler under bin/
    # still needs its plugin's assets and directory identity. This adds no write root;
    # the kernel still denies every write for a read-only child.
    anchor = (Path(working_directory).resolve() if working_directory is not None else
              roots[0] if roots else Path(str(argv[0]) if argv else tempfile.gettempdir()).resolve().parent)
    runner = JobRunner(
        ExecutionPolicy(
            workspace_root=anchor,
            writable_roots=roots,
            allow_network_egress=False,
            network_isolation_mode="auto",
            read_roots=(*default_read_roots(), *_program_read_roots(list(argv)), anchor),
        )
    )
    if allow_network:
        return runner._filesystem_only_isolation_prefix(list(argv), roots)
    return runner._kernel_network_isolation_prefix(list(argv), roots)


def _backend_label(wrapped: list[str], bare: list[str]) -> str:
    if not wrapped or wrapped == bare:
        return ""
    return f"kernel:{os.path.basename(str(wrapped[0]))}"


def _windows_broker_path() -> Path | None:
    """Resolve only the packaged broker or an explicit test/development broker path."""

    override = str(os.environ.get("VOOL_WINDOWS_BROKER") or "").strip()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute() or candidate.suffix.lower() != ".exe":
            return None
        try:
            candidate = candidate.resolve()
        except (OSError, RuntimeError):
            return None
        return candidate if candidate.is_file() else None

    try:
        executable = Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return None
    # The supported bundle layout is <bundle-root>\python\python.exe and
    # <bundle-root>\native\broker\vool-broker.exe. No arbitrary code-root fallback is used.
    if executable.parent.name.lower() != "python":
        return None
    candidate = executable.parent.parent / "native" / "broker" / "vool-broker.exe"
    return candidate if candidate.is_file() else None


def _windows_runtime_read_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in (sys.prefix, sys.base_prefix, str(Path(sys.executable).resolve().parent)):
        try:
            root = Path(value).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    return tuple(roots)


def _windows_plugin_identity(plugin_root: Path, executable: Path, plugin_id: str) -> tuple[str, str]:
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest_bytes = manifest.read_bytes()
        manifest_json = json.loads(manifest_bytes.decode("utf-8"))
        version = str(manifest_json.get("version") or "unknown")
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        return version, digest
    except (OSError, UnicodeError, ValueError, AttributeError):
        # Test-only/unpackaged plugin roots still get a stable identity bound to the exact
        # executable bytes; a missing manifest is never treated as a trusted package identity.
        try:
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        except OSError:
            digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()
        return "unmanifested", digest


def _windows_broker_environment() -> dict[str, str]:
    system_root = str(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "").strip()
    values: dict[str, str] = {}
    if system_root:
        values["SystemRoot"] = system_root
        values["WINDIR"] = system_root
    values["PATH"] = os.pathsep.join(
        value for value in (
            str(Path(sys.executable).resolve().parent),
            str(Path(system_root) / "System32") if system_root else "",
            system_root,
        ) if value
    )
    for name in ("TEMP", "TMP"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            values[name] = value
    return values


def _run_windows_broker(
    *,
    intent: str,
    arguments: dict[str, Any],
    handler: dict[str, Any],
    plugin_root: Path,
    executable: Path,
    scratch_dir: str,
    timeout_seconds: float,
    allow_network: bool,
    read_only: bool,
    env_allowlist: tuple[str, ...],
    secrets: dict[str, str] | None,
    schema: dict[str, Any],
) -> PluginToolResult:
    broker = _windows_broker_path()
    if broker is None:
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error=(
                "Native Windows plugin broker executable was not found; the child was not started."
            ),
            observation={"intent": intent},
        )

    try:
        if not str(scratch_dir or "").strip():
            raise ConfinementUnavailableError(
                "Native Windows plugin execution requires an explicit isolated scratch root; the child was not started."
            )
        scratch = Path(scratch_dir).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        runtime_roots = _windows_runtime_read_roots()
        plugin_id = intent.split(".", 1)[0] or "unknown-plugin"
        plugin_version, plugin_digest = _windows_plugin_identity(plugin_root, executable, plugin_id)
        child_env = build_child_env(
            env_allowlist=env_allowlist,
            secrets=secrets,
            scratch_dir=str(scratch),
        )
        environment_allowlist = sorted(set(env_allowlist) | set(child_env))
        # The Windows broker grants the plugin package read/execute access only. Every
        # mutation in this bounded integration is directed to the isolated scratch root;
        # the package itself is never a writable grant.
        writable_roots = [] if read_only else [scratch]

        script_args = [str(executable)]
        script_args.extend(str(value) for value in (handler.get("args") or ()))
        broker_executable = executable
        if executable.suffix.lower() != ".exe":
            broker_executable = Path(sys.executable).resolve()
            if not broker_executable.is_file():
                raise ConfinementUnavailableError(
                    "Native Windows plugin interpreter was not found; the child was not started."
                )
            script_args = [str(executable), *[str(value) for value in (handler.get("args") or ())]]

        read_roots = [plugin_root.resolve(), scratch, *runtime_roots]
        request_payload = {
            "schema": "vool.plugin_broker.v1",
            "plugin_id": plugin_id,
            "plugin_version": plugin_version,
            "plugin_digest": plugin_digest,
            "operation_id": f"{plugin_id}-{uuid.uuid4().hex}",
            "executable_path": str(broker_executable),
            "arguments": script_args,
            "working_directory": str(plugin_root.resolve()),
            "plugin_root": str(plugin_root.resolve()),
            "scratch_root": str(scratch),
            "read_roots": [str(root) for root in read_roots],
            "writable_roots": [str(root) for root in writable_roots],
            "allow_network": bool(allow_network),
            "timeout_ms": max(1_000, int(float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS) * 1_000)),
            "environment_allowlist": environment_allowlist,
            "environment": child_env,
            "stdin": json.dumps(
                {
                    "intent": intent,
                    "arguments": json_schema_lite.apply_defaults(arguments, schema),
                    "context": {
                        "scratch_dir": str(scratch),
                        "secrets": sorted((secrets or {}).keys()),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        completed = subprocess.run(
            [str(broker), "plugin"],
            input=json.dumps(request_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS) + 10.0),
            cwd=str(plugin_root.resolve()),
            env=_windows_broker_environment(),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return PluginToolResult(
            ok=False,
            status="timeout",
            error=f"Windows plugin broker exceeded {timeout_seconds}s",
            observation={"intent": intent},
            confinement="windows:appcontainer_job_object",
        )
    except (OSError, ValueError, RuntimeError, ConfinementUnavailableError) as exc:
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error=str(exc),
            observation={"intent": intent},
        )

    stdout = (completed.stdout or "").strip()
    try:
        broker_result = json.loads(stdout) if stdout else {}
    except ValueError:
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error=f"Windows plugin broker did not emit JSON: {stdout[:200]!r}",
            observation={"intent": intent},
        )
    if not isinstance(broker_result, dict):
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error="Windows plugin broker emitted JSON that is not an object",
            observation={"intent": intent},
        )

    evidence = broker_result.get("confinement")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    broker_error = str(broker_result.get("error") or "")
    if not evidence:
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error=broker_error or "Windows broker returned no confinement evidence.",
            observation={"intent": intent},
        )

    child_stdout = str(broker_result.get("child_stdout") or "").strip()
    child_stderr = str(broker_result.get("child_stderr") or "").strip()
    exit_code = int(broker_result.get("exit_code") or 0)
    confinement_label = "windows:appcontainer_job_object"
    if exit_code != 0 and not child_stdout:
        return PluginToolResult(
            ok=False,
            status="timeout" if bool(broker_result.get("timed_out")) else "handler_failed",
            error=broker_error or f"exit {exit_code}: {child_stderr[:500]}",
            observation={"intent": intent},
            confinement=confinement_label,
            confinement_evidence=evidence,
        )
    try:
        parsed = json.loads(child_stdout) if child_stdout else {}
    except ValueError:
        return PluginToolResult(
            ok=False,
            status="handler_protocol_error",
            error=f"handler did not emit one JSON object on stdout: {child_stdout[:200]!r}",
            confinement=confinement_label,
            confinement_evidence=evidence,
        )
    if not isinstance(parsed, dict):
        return PluginToolResult(
            ok=False,
            status="handler_protocol_error",
            error="handler emitted JSON that is not an object",
            confinement=confinement_label,
            confinement_evidence=evidence,
        )
    observation = parsed.get("observation")
    observation = dict(observation) if isinstance(observation, dict) else {}
    observation.setdefault("intent", intent)
    ok = bool(parsed.get("ok", True))
    return PluginToolResult(
        ok=ok,
        status=str(parsed.get("status") or ("executed" if ok else "failed")),
        text=str(parsed.get("text") or ""),
        observation=observation,
        resolved_target=str(parsed.get("resolved_target") or ""),
        error=str(parsed.get("error") or ""),
        confinement=confinement_label,
        confinement_evidence=evidence,
    )


def confined_argv(
    argv: list[str],
    *,
    writable_roots: tuple[Path, ...],
    allow_network: bool = False,
    mode: str = "auto",
    working_directory: Path | None = None,
) -> tuple[list[str], str]:
    """The argv to launch and a label naming how it is confined.

    ``mode="auto"`` (default) raises `ConfinementUnavailableError` when the host cannot enforce
    confinement. On POSIX only, ``mode="heuristic_only"`` is an explicit local-development
    opt-in that runs bare argv and labels the weaker result. Native Windows never takes it.
    """

    clean_mode = str(mode or "auto").strip().lower()
    if clean_mode not in CONFINEMENT_MODES:
        clean_mode = "auto"
    bare = [str(a) for a in argv]
    wrapped = _kernel_confinement_prefix(
        bare, writable_roots=writable_roots, allow_network=allow_network,
        working_directory=working_directory,
    )
    if wrapped is not None:
        return list(wrapped), _backend_label(list(wrapped), bare) or "kernel:unknown"
    if os.name == "nt":
        # The Windows desktop cannot treat an explicit config opt-in as a sandbox.
        # Until a native broker enforces filesystem, process, and network policy,
        # plugin and MCP handlers must not run as ordinary user processes.
        raise ConfinementUnavailableError(
            "Native Windows has no OS-enforced plugin execution broker; the child was not started."
        )
    if clean_mode == "heuristic_only":
        return bare, "heuristic_only"
    raise ConfinementUnavailableError(
        "this host has no usable kernel confinement backend (sandbox-exec on macOS; bwrap, unshare "
        "or firejail on Linux), so the child was not started. Set confinement to 'heuristic_only' "
        "only as an explicit, informed local override."
    )


def kernel_confinement_available() -> bool:
    """Whether this host can confine a child at all (probed, cached by the sandbox backend)."""

    try:
        probe_root = Path(tempfile.gettempdir()).resolve()
        return _kernel_confinement_prefix(["true"], writable_roots=(probe_root,)) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_plugin_tool(
    *,
    intent: str,
    arguments: dict[str, Any],
    handler: dict[str, Any],
    schema: dict[str, Any],
    plugin_root: Path,
    env_allowlist: tuple[str, ...] = (),
    secrets: dict[str, str] | None = None,
    scratch_dir: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allow_network: bool = False,
    confinement: str = "auto",
    read_only: bool = False,
) -> PluginToolResult:
    """Validate, then run one plugin tool. Never raises; every failure is a structured result."""

    filled = json_schema_lite.apply_defaults(arguments, schema)
    errors = json_schema_lite.validate(filled, schema)
    if errors:
        return PluginToolResult(
            ok=False, status="invalid_arguments", error="; ".join(errors), observation={"intent": intent}
        )
    shape_problems = check_argument_shapes(filled, schema)
    if shape_problems:
        return PluginToolResult(
            ok=False,
            status="invalid_argument_shape",
            error="; ".join(shape_problems),
            observation={"intent": intent},
        )

    entry = str((handler or {}).get("entry") or "").strip()
    if not entry:
        return PluginToolResult(ok=False, status="handler_missing", error="handler declares no entry")
    root = Path(plugin_root).resolve()
    executable = (root / entry).resolve()
    # A handler path must stay inside its own plugin: `entry: "../../../bin/sh"` would otherwise
    # run anything on the machine under the plugin's declared permissions.
    if not str(executable).startswith(str(root) + os.sep):
        return PluginToolResult(
            ok=False, status="handler_escaped", error=f"handler entry {entry!r} resolves outside the plugin"
        )
    if not executable.is_file():
        return PluginToolResult(ok=False, status="handler_missing", error=f"no handler at {entry}")

    # READ-ONLY CONFINEMENT (2026-09-02 amendment): a contract that claims read_only gets NO
    # writable root at all -- the kernel confinement makes the claim mechanical, and a
    # misdeclared plugin that tries to mutate local state fails inside the child instead of
    # publishing success. The executor additionally scan-checks the workspace afterwards.
    writable: list[Path] = [] if read_only else [root]
    if scratch_dir and not read_only:
        try:
            Path(scratch_dir).mkdir(parents=True, exist_ok=True)
            writable.append(Path(scratch_dir).resolve())
        except OSError:
            pass

    if os.name == "nt":
        return _run_windows_broker(
            intent=intent,
            arguments=filled,
            handler=handler,
            plugin_root=root,
            executable=executable,
            scratch_dir=scratch_dir,
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
            read_only=read_only,
            env_allowlist=env_allowlist,
            secrets=secrets,
            schema=schema,
        )

    bare_argv = [str(executable), *[str(a) for a in (handler.get("args") or ())]]
    try:
        argv, confinement_label = confined_argv(
            bare_argv,
            writable_roots=tuple(writable),
            allow_network=bool(allow_network),
            mode=str(confinement or "auto"),
            working_directory=root,
        )
    except ConfinementUnavailableError as exc:
        return PluginToolResult(
            ok=False,
            status="confinement_unavailable",
            error=str(exc),
            observation={"intent": intent},
        )

    payload = {
        "intent": intent,
        "arguments": filled,
        "context": {"scratch_dir": scratch_dir, "secrets": sorted((secrets or {}).keys())},
    }
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)),
            cwd=str(root),
            env=build_child_env(env_allowlist=env_allowlist, secrets=secrets, scratch_dir=scratch_dir),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return PluginToolResult(
            ok=False,
            status="timeout",
            error=f"handler exceeded {timeout_seconds}s",
            confinement=confinement_label,
        )
    except OSError as exc:
        return PluginToolResult(ok=False, status="handler_failed", error=str(exc), confinement=confinement_label)

    stdout = (completed.stdout or "")[:_MAX_OUTPUT_BYTES].strip()
    if completed.returncode != 0 and not stdout:
        stderr = (completed.stderr or "").strip()[:500]
        return PluginToolResult(
            ok=False,
            status="handler_failed",
            error=f"exit {completed.returncode}: {stderr}",
            observation={"intent": intent},
            confinement=confinement_label,
        )
    try:
        parsed = json.loads(stdout) if stdout else {}
    except ValueError:
        return PluginToolResult(
            ok=False,
            status="handler_protocol_error",
            error=f"handler did not emit one JSON object on stdout: {stdout[:200]!r}",
            confinement=confinement_label,
        )
    if not isinstance(parsed, dict):
        return PluginToolResult(
            ok=False,
            status="handler_protocol_error",
            error="handler emitted JSON that is not an object",
            confinement=confinement_label,
        )

    observation = parsed.get("observation")
    observation = dict(observation) if isinstance(observation, dict) else {}
    observation.setdefault("intent", intent)
    ok = bool(parsed.get("ok", True))
    return PluginToolResult(
        ok=ok,
        status=str(parsed.get("status") or ("executed" if ok else "failed")),
        text=str(parsed.get("text") or ""),
        observation=observation,
        resolved_target=str(parsed.get("resolved_target") or ""),
        error=str(parsed.get("error") or ""),
        confinement=confinement_label,
    )


__all__ = [
    "CONFINEMENT_MODES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ConfinementUnavailableError",
    "PluginToolResult",
    "build_child_env",
    "check_argument_shapes",
    "confined_argv",
    "kernel_confinement_available",
    "run_plugin_tool",
]
