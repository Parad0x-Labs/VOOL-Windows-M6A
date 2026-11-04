"""Windows job confinement backend (docs/WINDOWS_CONFINEMENT.md): the interface, not the backend.

Chunk 0 of the phased plan. Builders fill in `launcher_path`, `probe` and `confinement_prefix`.
Until they do, every function here reports the backend UNAVAILABLE, so `JobRunner` keeps refusing
confined jobs on Windows (`KernelIsolationUnavailableError`). Unknown or unproven always means
unavailable: there is no path in this module that returns an argv that runs unconfined.
"""

from __future__ import annotations

import hashlib
import json
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
    read_roots: tuple[str, ...] = ()
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
        for key in ("write_roots", "read_roots", "deny_write_paths", "deny_all_paths"):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise PolicyError(f"{key} must be a list of strings")
        policy = cls(
            write_roots=tuple(data["write_roots"]),
            temp_dir=data["temp_dir"],
            status_path=data["status_path"],
            read_roots=tuple(data.get("read_roots", [])),
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


def probe(project_root: Path) -> bool:
    """Whether the backend demonstrably confines a no-op job on this host. Stub: always False.

    The real probe must RUN a confined child and check its result; finding the file is not proof."""
    return False


def confinement_prefix(
    argv: list[str], policy: WindowsConfinementPolicy, *, project_root: Path, policy_path: Path
) -> list[str] | None:
    """The argv that runs `argv` confined by `policy`, or None when the backend is unavailable.

    None makes the caller refuse the job. Stub: always None."""
    policy.validate()
    return None
