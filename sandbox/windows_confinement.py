"""Windows job confinement backend (docs/WINDOWS_CONFINEMENT.md): the interface, not the backend.

Chunk 0 of the phased plan. Builders fill in `launcher_path`, `probe` and `confinement_prefix`.
Until they do, every function here reports the backend UNAVAILABLE, so `JobRunner` keeps refusing
confined jobs on Windows (`KernelIsolationUnavailableError`). Unknown or unproven always means
unavailable: there is no path in this module that returns an argv that runs unconfined.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "vool.windows_confinement.v1"
STATUS_SCHEMA = "vool.windows_confinement.status.v1"

#: Launcher exit codes (docs/WINDOWS_CONFINEMENT.md section 2). A child may exit with any code,
#: including these, so a code alone proves nothing: only the launcher's status file (outside every
#: writable root, so the confined child cannot write it) says whether the child started.
EXIT_NEW_REPOSITORY = 123
EXIT_TIMEOUT = 124
EXIT_SETUP_FAILED = 125
EXIT_POLICY_INVALID = 126
REFUSED_MARKER = "VOOLCONFINE-REFUSED:"  # human-readable stderr line only; never trusted

_KEYS = (
    "schema",
    "write_roots",
    "read_roots",
    "job_read_roots",
    "deny_write_paths",
    "deny_all_paths",
    "deny_write_git",
    "network",
    "max_seconds",
    "memory_mb",
    "max_processes",
    "temp_dir",
    "status_path",
)


class PolicyError(ValueError):
    """The policy breaks a schema rule; the launcher would refuse it with exit 126."""


#: Win32 maps these names to devices in any folder, with or without an extension.
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"] + [f"{kind}{n}" for kind in ("COM", "LPT") for n in "123456789"]
)
_BAD_SEGMENT_CHARS = frozenset('<>"|?*~:/')
_DRIVE_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _check_path(value: str, what: str) -> str:
    """One canonical spelling only: Win32 rewrites trailing dots and spaces, `.` segments, 8.3 names
    and device names before it opens a path, so a lexical containment check is only sound when no
    such spelling gets in. The C# twin is `VoolConfine.Core.Policy.CheckPath`; keep them identical."""
    text = str(value)
    if text.startswith(("\\\\", "//")):
        raise PolicyError(f"{what} must be a drive-letter path, not a UNC or device path: {text!r}")
    head = text[:3]
    if len(head) < 3 or head[0] not in _DRIVE_LETTERS or head[1:] != ":\\":
        raise PolicyError(f"{what} must be an absolute drive-letter path with backslashes: {text!r}")
    if text == head:
        return text
    for part in text[3:].split("\\"):
        if part in ("", ".", ".."):
            raise PolicyError(f"{what} must be resolved (no empty, '.' or '..' segment): {text!r}")
        if part[-1] in ". ":
            raise PolicyError(f"{what} has a segment ending in a dot or space: {text!r}")
        if any(c in _BAD_SEGMENT_CHARS or ord(c) < 32 for c in part):
            raise PolicyError(f"{what} has a short name, stream or reserved character: {text!r}")
        stem = part.split(".", 1)[0].rstrip(" ")
        if "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in stem) in _DEVICE_NAMES:
            raise PolicyError(f"{what} names a device: {text!r}")
    return text


def _inside(child: str, root: str) -> bool:
    """Whether child is root or under it, case-insensitively under EITHER upper- or lower-casing (NTFS
    folds case with its own table). "Inside" refuses a status_path, so a disagreement only refuses."""
    c = child.rstrip("\\")
    r = root.rstrip("\\")
    for fold in (str.upper, str.lower, str.casefold):
        fc, fr = fold(c), fold(r)
        if fc == fr or fc.startswith(fr + "\\"):
            return True
    return False


@dataclass(frozen=True)
class WindowsConfinementPolicy:
    write_roots: tuple[str, ...]
    temp_dir: str
    status_path: str
    #: Shared read roots (the interpreter and its base): granted to the package SID, so every
    #: confined job can read them. Never a workspace or anything a user put there.
    read_roots: tuple[str, ...] = ()
    #: This job's own read roots (its workspace, the policy's read roots): each one is granted to its
    #: own read capability, which only a job whose policy names that root carries.
    job_read_roots: tuple[str, ...] = ()
    deny_write_paths: tuple[str, ...] = ()
    deny_all_paths: tuple[str, ...] = ()
    deny_write_git: bool = True
    network: bool = False
    max_seconds: int = 120
    memory_mb: int = 512
    max_processes: int = 64
    _validated: bool = field(default=False, init=False, repr=False, compare=False)

    def validate(self) -> None:
        if not self.write_roots:
            raise PolicyError("write_roots must not be empty")
        roots = [_check_path(p, "write_roots entry") for p in self.write_roots]
        for p in self.read_roots:
            _check_path(p, "read_roots entry")
        for p in self.job_read_roots:
            _check_path(p, "job_read_roots entry")
        for p in self.deny_write_paths:
            _check_path(p, "deny_write_paths entry")
        for p in self.deny_all_paths:
            _check_path(p, "deny_all_paths entry")
        temp = _check_path(self.temp_dir, "temp_dir")
        if not any(_inside(temp, root) for root in roots):
            raise PolicyError("temp_dir must sit inside a write root")
        status = _check_path(self.status_path, "status_path")
        if any(_inside(status, root) for root in roots):
            raise PolicyError("status_path must sit outside every write root")
        for name in ("max_seconds", "memory_mb", "max_processes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PolicyError(f"{name} must be a positive integer")
        for name in ("deny_write_git", "network"):
            if not isinstance(getattr(self, name), bool):
                raise PolicyError(f"{name} must be true or false")

    def to_json(self) -> str:
        self.validate()
        return json.dumps(
            {
                "schema": SCHEMA,
                "write_roots": list(self.write_roots),
                "read_roots": list(self.read_roots),
                "job_read_roots": list(self.job_read_roots),
                "deny_write_paths": list(self.deny_write_paths),
                "deny_all_paths": list(self.deny_all_paths),
                "deny_write_git": self.deny_write_git,
                "network": self.network,
                "max_seconds": self.max_seconds,
                "memory_mb": self.memory_mb,
                "max_processes": self.max_processes,
                "temp_dir": self.temp_dir,
                "status_path": self.status_path,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> WindowsConfinementPolicy:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PolicyError(f"policy is not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        unknown = sorted(set(data) - set(_KEYS))
        if unknown:
            raise PolicyError(f"unknown policy keys: {unknown}")
        if data.get("schema") != SCHEMA:
            raise PolicyError(f"schema must be {SCHEMA!r}")
        missing = [key for key in ("write_roots", "temp_dir", "status_path") if key not in data]
        if missing:
            raise PolicyError(f"missing policy keys: {missing}")
        for key in ("write_roots", "read_roots", "job_read_roots", "deny_write_paths", "deny_all_paths"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise PolicyError(f"{key} must be a list of strings")
        policy = cls(
            write_roots=tuple(data["write_roots"]),
            temp_dir=data["temp_dir"],
            status_path=data["status_path"],
            read_roots=tuple(data.get("read_roots", [])),
            job_read_roots=tuple(data.get("job_read_roots", [])),
            deny_write_paths=tuple(data.get("deny_write_paths", [])),
            deny_all_paths=tuple(data.get("deny_all_paths", [])),
            deny_write_git=data.get("deny_write_git", True),
            network=data.get("network", False),
            max_seconds=data.get("max_seconds", 120),
            memory_mb=data.get("memory_mb", 512),
            max_processes=data.get("max_processes", 64),
        )
        policy.validate()
        return policy


@dataclass(frozen=True)
class LaunchStatus:
    """The launcher's status file (docs/WINDOWS_CONFINEMENT.md section 2): the only trusted record."""

    child_started: bool
    timed_out: bool = False
    exit_code: int | None = None
    #: Why setup failed when the child never started (human-readable; refusal either way).
    error: str | None = None
    #: The job limit that ended the child, if one did: "time", "memory" or "processes".
    limit: str | None = None
    #: Repository layouts (a `.git` dir, or a bare repo) that appeared under a write root during the
    #: job. Non-empty means the result is refused (the launcher also exits 123).
    new_repositories: tuple[str, ...] = ()
    #: True only after the post-job repository scan COMPLETED (a killed launcher never gets there).
    #: Without it, `new_repositories` being empty proves nothing, so the caller refuses the result
    #: or runs the `scan` form itself.
    repositories_scanned: bool = False


def read_status(path: Path) -> LaunchStatus | None:
    """The launcher's status, or None when it is missing or malformed (the caller then refuses)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("schema") != STATUS_SCHEMA
        or not isinstance(data.get("child_started"), bool)
    ):
        return None
    timed_out = data.get("timed_out", False)
    exit_code = data.get("exit_code")
    error = data.get("error")
    limit = data.get("limit")
    if not isinstance(timed_out, bool) or not (
        exit_code is None or (isinstance(exit_code, int) and not isinstance(exit_code, bool))
    ):
        return None
    if not (error is None or isinstance(error, str)) or limit not in (None, *LIMITS):
        return None
    repos = data.get("new_repositories", [])
    if not isinstance(repos, list) or not all(isinstance(item, str) for item in repos):
        return None
    scanned = data.get("repositories_scanned", False)
    if not isinstance(scanned, bool):
        return None
    return LaunchStatus(data["child_started"], timed_out, exit_code, error, limit, tuple(repos), scanned)


#: AppContainer identity (docs/WINDOWS_CONFINEMENT.md section 2, "Identity").
PROFILE_NAME = "VOOL.Confine.v1"
LIMITS = ("time", "memory", "processes")


def root_capability_name(root: str) -> str:
    """The capability name a write root is granted to: one per root, so job A never holds root B."""
    # ASCII-only upper-casing: Python's str.upper() and .NET's ToUpperInvariant disagree outside
    # ASCII ("ß" -> "SS" here only), and both sides must hash the same bytes.
    trimmed = str(root).replace("/", "\\").rstrip("\\")
    normalized = "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in trimmed)
    return "vool.root." + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def launcher_path(project_root: Path) -> Path | None:
    """The pinned VoolConfine.exe (sandbox/windows_confinement_pin.py), never one from PATH."""
    from sandbox.windows_confinement_pin import verified_launcher

    return verified_launcher(project_root)


#: The probe's confined no-op. It must write inside its workspace, and it must FAIL to write
#: outside it and to reach loopback, or the backend is not confining anything and is unusable.
_PROBE_CHILD = (
    "import os, socket, sys\n"
    "ws, outside, port = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
    "with open(os.path.join(ws, 'probe-ok'), 'w') as f: f.write('ok')\n"
    "try:\n"
    "    with open(os.path.join(outside, 'escaped'), 'w') as f: f.write('x')\n"
    "    sys.exit(3)\n"
    "except OSError: pass\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', port), timeout=3).close()\n"
    "    sys.exit(4)\n"
    "except OSError: pass\n"
    "sys.exit(0)\n"
)


def interpreter_read_roots() -> tuple[str, ...]:
    """The folders the running interpreter must read to start: its venv and its base install."""
    import os
    import sys

    roots: list[str] = []
    for candidate in (sys.prefix, sys.base_prefix, os.path.dirname(sys.executable or "")):
        text = str(candidate or "").strip()
        if text:
            resolved = str(Path(text).resolve())
            if resolved not in roots:
                roots.append(resolved)
    return tuple(roots)


def _no_window_flags() -> int:
    import subprocess

    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


#: Variables the .NET host reads to load extra code (profilers, startup hooks, host overrides).
#: The pin proves the launcher's files, not what these would make it load, so every launch drops them.
_LOADER_ENV_PREFIXES = ("CORECLR_", "COR_", "COMPLUS_", "DOTNET_", "COREHOST_")


def launcher_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """*env* (default: this process's) without any variable that makes the .NET host load code."""
    import os

    source = os.environ if env is None else env
    return {key: value for key, value in source.items() if not key.upper().startswith(_LOADER_ENV_PREFIXES)}


_VOOL_JOB = None
_VOOL_JOB_LOCK = threading.Lock()


def _vool_job():
    """VOOL's own kill-on-close job for its launchers, created once and held for this process's life,
    so no launcher outlives VOOL (and holds the program folder open through an upgrade). None off
    Windows. Failing to create it raises, and the caller refuses the launch."""
    import os

    global _VOOL_JOB
    if os.name != "nt":
        return None
    with _VOOL_JOB_LOCK:
        if _VOOL_JOB is None:
            from core.windows_job import new_kill_on_close_job

            _VOOL_JOB = new_kill_on_close_job()
        return _VOOL_JOB


def launcher_creationflags() -> int:
    """No console, its own process group, and on Windows SUSPENDED until `adopt_launcher` runs."""
    import os

    flags = _no_window_flags()
    if os.name == "nt":
        from core.windows_job import CREATE_SUSPENDED

        flags |= CREATE_SUSPENDED
    return flags


def adopt_launcher(pid: int) -> None:
    """Put a launcher started with `launcher_creationflags` in VOOL's job, then let it run.

    Raises when it can't, and the caller kills the launcher (still suspended, so it ran nothing)."""
    import os

    job = _vool_job()
    if job is not None:
        job.assign(pid)
    if os.name == "nt":
        if job is None:  # pragma: no cover - _vool_job raises rather than return None on Windows
            raise OSError("VOOL's launcher job is unavailable")
        from core.windows_job import resume_suspended

        resume_suspended(pid)


def _run_launcher(argv: list[str], *, timeout: float) -> int:
    """Run one launcher invocation in VOOL's job with the scrubbed environment; its exit code."""
    import subprocess

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=launcher_environment(),
        creationflags=launcher_creationflags(),
    )
    try:
        adopt_launcher(process.pid)
        process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return int(process.returncode)


def launcher_form(launcher: Path | str, form: str, policy_path: Path, *, timeout: float = 300) -> bool:
    """Run the launcher's unconfined `grant` or `revoke` form for a policy. True only on exit 0.

    The run form only VERIFIES ACLs, and a miss there is 125, so a caller grants first. `grant`
    and `revoke` are run by VOOL, never by a job, and an error, a timeout or any other exit code
    counts as not done."""
    import subprocess

    if form not in ("grant", "revoke"):
        raise ValueError(f"unknown launcher form {form!r}")
    try:
        return _run_launcher([str(launcher), form, "--policy", str(policy_path)], timeout=timeout) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def launcher_scan(launcher: Path | str, policy_path: Path, *, timeout: float = 300) -> int | None:
    """Run the launcher's unconfined `scan` form: the repository scan alone, no child. It rewrites the
    status file's `new_repositories` and `repositories_scanned`. Its exit code (0 none, 123 found,
    125 unfinished), or None when it could not run or didn't finish in time."""
    import subprocess

    try:
        return _run_launcher([str(launcher), "scan", "--policy", str(policy_path)], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def probe(project_root: Path) -> bool:
    """Whether the backend demonstrably confines a no-op job on this host.

    Finding the launcher is not proof, so this grants a fresh probe-only workspace, RUNS a confined
    child in it, then revokes the grant. It accepts the backend only when all of these hold:
    - the launcher's status file says the child started and exited 0;
    - the child's write inside the workspace landed;
    - its write outside the workspace and its loopback connect were both refused;
    - the grant and the revoke both exited 0.
    Anything else, including an exception, means unavailable."""
    import shutil
    import socket
    import sys
    import tempfile

    launcher = launcher_path(project_root)
    if launcher is None:
        return False
    stage = workspace = None
    listener = None
    granted_policy: Path | None = None
    revoked = True
    try:
        stage = Path(tempfile.mkdtemp(prefix="vool-confine-probe-")).resolve()
        workspace = Path(tempfile.mkdtemp(prefix="vool-confine-probe-ws-")).resolve()
        temp_dir = workspace / ".sandbox-tmp"
        temp_dir.mkdir()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        policy = WindowsConfinementPolicy(
            write_roots=(str(workspace),),
            read_roots=interpreter_read_roots(),
            temp_dir=str(temp_dir),
            status_path=str(stage / "status.json"),
            max_seconds=30,
        )
        argv = confinement_prefix(
            [str(Path(sys.executable).resolve()), "-I", "-c", _PROBE_CHILD, str(workspace), str(stage), str(port)],
            policy,
            project_root=project_root,
            policy_path=stage / "policy.json",
        )
        if argv is None:
            return False
        revoked = False
        granted_policy = stage / "policy.json"
        if not launcher_form(argv[0], "grant", granted_policy, timeout=60):
            return False
        returncode = _run_launcher(argv, timeout=60)
        status = read_status(stage / "status.json")
        revoked = launcher_form(argv[0], "revoke", granted_policy, timeout=60)
        granted_policy = None
        return (
            revoked
            and returncode == 0
            and status is not None
            and status.child_started
            and not status.timed_out
            and not status.new_repositories
            and status.exit_code == 0
            and (workspace / "probe-ok").is_file()
            and not (stage / "escaped").exists()
        )
    except Exception:
        return False
    finally:
        if granted_policy is not None and not revoked:
            launcher_form(launcher, "revoke", granted_policy, timeout=60)
        if listener is not None:
            listener.close()
        for folder in (stage, workspace):
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)


def confinement_prefix(
    argv: list[str], policy: WindowsConfinementPolicy, *, project_root: Path, policy_path: Path
) -> list[str] | None:
    """The argv that runs `argv` confined by `policy`, or None when the backend is unavailable.

    None makes the caller refuse the job. This writes `policy` to `policy_path`. That path must sit
    outside every write root, so the confined child can never rewrite the rules it runs under. A
    status file already at `status_path` is refused too: an earlier run's record must never be read
    as this one's. On Windows argv[0] must be an absolute `.exe`; the launcher refuses anything else
    (a `.cmd` or `.bat` would run through cmd.exe's own quoting) with 126."""
    import os

    policy.validate()
    launcher = launcher_path(project_root)
    if launcher is None or not argv or not os.path.isabs(str(argv[0])):
        return None
    if os.name == "nt" and not str(argv[0]).lower().endswith(".exe"):
        return None
    policy_text = str(policy_path)
    if any(_inside(policy_text, root) for root in policy.write_roots):
        return None
    if any(_inside(policy.status_path, root) for root in policy.write_roots):
        return None
    if os.path.lexists(policy.status_path):
        return None
    try:
        policy_path.write_text(policy.to_json(), encoding="utf-8")
    except OSError:
        return None
    return [str(launcher), "--policy", policy_text, "--", *[str(item) for item in argv]]
