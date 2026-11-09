from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from sandbox.container_adapter import ExecutionResult
from sandbox.network_guard import command_uses_network
from sandbox.resource_limits import (
    ExecutionPolicy,
    normalize_policy,
    path_args_within_roots,
    path_within_roots,
)

#: Files to look at before deciding a writable root has no hard links. A root with more
#: entries than this is not a sane sandbox target, and answering "probably fine, I stopped
#: looking" is the failure mode this whole lane exists to delete — so the cap refuses.
_HARDLINK_SCAN_FILE_LIMIT = 200_000



def _is_windows_platform() -> bool:
    """Indirection over ``os.name == "nt"`` so tests can simulate "running on Windows" for one
    call without mutating the real ``os.name`` -- ``pathlib.Path`` itself branches on ``os.name``
    to choose ``WindowsPath``/``PosixPath``, so patching the global attribute directly breaks
    every OTHER ``Path(...)`` construction for the duration of the patch, including ones made
    deep inside pytest's own failure reporting (measured: full run 36063857499 shards 5/9 died
    with ``cannot instantiate 'WindowsPath'`` inside ``_pytest``'s traceback formatter while
    rendering an assertion failure, because a test had flipped the global)."""
    return os.name == "nt"


class _JobCancelledError(Exception):
    """Internal: the cancel event fired while the job was running."""


class HardLinkedWritableRootError(ValueError):
    """A writable root contains a file with more than one name on the filesystem.

    Path-based kernel confinement — Seatbelt, and bwrap's bind mounts — decides by PATH. A
    hard link is a second path to the same inode, so `workspace/innocent.txt` can be the
    operator's `~/Desktop/keys.txt` and the kernel will allow the write, correctly, because
    the path it was asked about really is inside the workspace. Measured on macOS 26.5.1:
    writing through a pre-planted hard link succeeds under `(allow file-write* (subpath ws))`,
    and adding `(deny file-link)` does not change it — that rule governs CREATING links.

    What the kernel does close is the other half: a confined job cannot create a hard link at
    all (`os.link` fails under the profile), so a link inside a writable root was put there by
    something that was already running unconfined. This check is what notices that, before the
    job starts, from `st_nlink` — an OS fact about an inode, not a guess about a string.
    """


class KernelIsolationUnavailableError(ValueError):
    """No isolation backend on this host can actually enforce no-egress.

    Distinct from the other refusals `run` raises so a caller can tell "this host cannot isolate"
    from "policy forbids this command". Subclasses ValueError deliberately: every existing caller
    catches ValueError and keeps working unchanged.
    """


class NestedGitMetadataError(ValueError):
    """A confined Windows job created a repository layout under a writable root.

    The Windows launcher denies writes only to `.git` folders that existed when the job started.
    It can't deny a name that doesn't exist yet, so a job could `git init` a nested repository and
    plant a hook or a `core.fsmonitor` command for git to run later outside the sandbox. (The
    Mac's Seatbelt profile denies the path pattern itself.) The launcher scans the roots before
    and after the job and reports new layouts in its status record, exiting 123. The runner
    refuses that result. The layout is left where it is, for the operator to inspect.
    """


#: Phase 1 of Windows confinement runs Python and pytest only (jobs/broker/confinement-B/PLAN.md).
#: Every other program (git, npm/node, cargo, cmd, PowerShell…) is refused until its toolchain is
#: proven inside the AppContainer. A tool that half-works under confinement must never be reported
#: as a confined run.
_WINDOWS_PYTHON_NAMES = frozenset({"python", "python3", "py"})
_WINDOWS_PYTEST_NAMES = frozenset({"pytest", "py.test"})

#: The only modules a phase-1 Python job may run with `-m`: pytest, and the two stdlib runners
#: VOOL's own builder uses to verify generated apps (`-m unittest`, `-m compileall -q`; see
#: core/agent_runtime/builder/app_builder.py and builder/controller.py). Neither installs anything.
#: Every other module is refused: pip, ensurepip, venv and ruff are tools the 0.7.1 Windows limits
#: say VOOL refuses. Names are matched exactly, as CPython's import does.
_WINDOWS_PYTHON_MODULES = frozenset({"pytest", "unittest", "compileall"})

#: CPython flags that take no argument and neither run a module nor read code from stdin. They
#: may come before the script, `-c` or an allowed `-m`, alone or combined (`-IB`). Every other flag
#: is refused: `-i` (a REPL on stdin), `-W` (a warning category is imported by name), `-X`
#: (implementation options, some of which import a module), `-h`/`-V` and every long option.
_WINDOWS_PYTHON_PLAIN_FLAGS = frozenset("bBdEIOPqRsSuvx")

#: CPython flags that take an argument, either the rest of the cluster (`-mpip`) or the next
#: argv item. Only `-c` and an allowed `-m` are accepted; knowing the others keeps the parser in step
#: with CPython so that `-W x -m pip` is read as CPython reads it.
_CPYTHON_FLAGS_WITH_ARGUMENT = frozenset("cmWX")


def _windows_python_args_refusal(args: list[str]) -> str | None:
    """Why phase 1 refuses this Python command line, or None when it runs.

    `args` is everything after the interpreter. It is read the way CPython reads its own
    command line: flags come first, alone or combined in one item; a flag that takes an argument
    takes the rest of its item or else the next item; `-c` and `-m` end the options; the first
    item that does not start with `-` is the script and ends the options too. Accepted forms:
    a `.py` script, `-c <code>` and `-m` with a module in `_WINDOWS_PYTHON_MODULES` (pytest,
    unittest, compileall), each after plain flags only. Anything else,
    including no program at all (`python` reads stdin), `-` (stdin), `--`, a long option or a
    flag this parser does not know, is refused. Unknown means refused.
    """
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--":
            return "'--' (an end-of-options marker)"
        if item == "-":
            return "'-' (a program read from standard input)"
        if not item.startswith("-"):
            name = item.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if item and name.lower().endswith(".py") and len(name) > 3:
                return None
            return f"'{item}' (only a .py script runs; not a folder, zip or wheel)"
        if item.startswith("--"):
            return f"the option '{item}'"
        cluster = item[1:]
        for position, flag in enumerate(cluster):
            if flag in _CPYTHON_FLAGS_WITH_ARGUMENT:
                value = cluster[position + 1:]
                if not value:
                    if index + 1 >= len(args):
                        return f"'-{flag}' with nothing after it"
                    value = args[index + 1]
                if flag == "c":
                    return None
                if flag == "m":
                    if value in _WINDOWS_PYTHON_MODULES:
                        return None
                    return f"'-m {value}'"
                return f"the option '-{flag}'"
            if flag not in _WINDOWS_PYTHON_PLAIN_FLAGS:
                return f"the option '-{flag}'"
        index += 1
    return "a Python command with no script, -c code or allowed -m module"

#: Policy and status files the Windows launcher was handed, keyed by the policy path in the argv,
#: so that `run` can read the status back and delete the staging folder. Only the Windows branch
#: of `_with_network_isolation` adds entries.
_WINDOWS_STAGED: dict[str, tuple[Path, str]] = {}
_WINDOWS_STAGED_LOCK = threading.Lock()

#: Grants the launcher's `grant` form already applied in this process, keyed by everything in a
#: policy that shapes ACLs. They are not revoked when a job ends or when VOOL exits: a workspace
#: root keeps its per-root capability grant (which only a confined job holding that root's
#: capability can use) until `VoolConfine.exe revoke` is run for it. Grant lifetime and cleanup
#: belong to the ACL chunk. The run form verifies them on every job, so a grant that was removed
#: since is a 125 there, never a silent pass, and that key is then forgotten so the next job grants
#: again.
_WINDOWS_GRANTED: set[str] = set()


def _repository_layouts(roots: tuple[Path, ...]) -> tuple[set[str], bool]:
    """Every repository layout under *roots*, and whether the walk finished inside the scan cap.

    A layout is any `.git` entry, folder or file (a `gitdir:` redirect), in any case and with
    the trailing dots or spaces Win32 strips. A bare repository counts too: a folder holding a
    `HEAD` file next to `objects` and `refs` folders. An unfinished walk proves nothing, so the
    caller refuses."""
    found: set[str] = set()
    seen = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            seen += len(dirnames) + len(filenames)
            if seen > _HARDLINK_SCAN_FILE_LIMIT:
                return found, False
            for name in (*dirnames, *filenames):
                if name.lower().rstrip(". ") == ".git":
                    found.add(os.path.normcase(os.path.join(dirpath, name)))
            lowered_dirs = {name.lower() for name in dirnames}
            if {"objects", "refs"} <= lowered_dirs and any(name.lower() == "head" for name in filenames):
                found.add(os.path.normcase(dirpath))
    return found, True


def _forget_staged_at_exit() -> None:
    """Remove staging folders that were built but never run (a caller that only wanted the argv)."""
    with _WINDOWS_STAGED_LOCK:
        for stage, _key in _WINDOWS_STAGED.values():
            shutil.rmtree(stage, ignore_errors=True)
        _WINDOWS_STAGED.clear()


atexit.register(_forget_staged_at_exit)


def _windows_path_text(path: Path | str) -> str:
    """A path as the Windows launcher's policy names it: absolute and resolved."""
    return str(Path(path).resolve())


def _windows_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


# Whether a backend actually works on this host, keyed by backend name. A backend being INSTALLED
# and a backend being PERMITTED are different questions, and only the second one matters:
# `unshare` ships in util-linux on every mainstream distro, so `shutil.which` finds it inside
# containers and CI runners that deny the namespace syscall it needs. Probed once per process --
# it is a property of the host and the probe costs a subprocess.
_BACKEND_USABLE: dict[str, bool] = {}


def _windows_probe_key(launcher: Path | str) -> str:
    """The `_BACKEND_USABLE` key the VoolConfine probe result is kept under."""
    return f"voolconfine:{launcher}"


def reset_isolation_backend_probe_cache() -> None:
    """Forget probe results. For tests that fake a backend's availability."""
    _BACKEND_USABLE.clear()
    _WINDOWS_GRANTED.clear()


def _backend_usable(name: str, probe_argv: list[str]) -> bool:
    """Whether *probe_argv* -- the wrapper plus a no-op command -- actually completes.

    Presence was the bug this exists to close. On a GitHub Actions runner `unshare` is on PATH and
    `unshare -n -- cat file` exits non-zero with `unshare failed: Operation not permitted`, so the
    wrapper's failure was reaching the user as though THEIR command had failed: `cat present.txt`
    came back `ok=False, status='command_failed'`. Nothing downstream could tell a broken command
    from a sandbox that was never able to run one.
    """
    cached = _BACKEND_USABLE.get(name)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            probe_argv,
            capture_output=True,
            timeout=10,
            shell=False,
            check=False,
        )
        usable = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        usable = False
    _BACKEND_USABLE[name] = usable
    return usable


#: Seatbelt's launcher, by absolute path. It used to be found with `shutil.which`, which walks
#: the PATH this process inherited: any writable directory early on that PATH could hold a
#: `sandbox-exec` that ignores its profile and runs the job bare, while every receipt still
#: said "confined". The binary is only trusted when it and its directory are root-owned and
#: not group- or world-writable (`_trusted_sandbox_exec`).
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Mach services a confined toolchain may look up, on top of Apple's own `system.sb` baseline
#: (logging, directory lookups, notifications). Everything else is denied: the keychain
#: (`com.apple.SecurityServer`), LaunchServices (`open -a`), Apple Events (`osascript`
#: driving another app), the pasteboard, the window server and launchd job submission all
#: answer over Mach, and `(allow default)` handed every one of them to the job.
_SEATBELT_TOOLCHAIN_MACH_SERVICES: tuple[str, ...] = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.system.notification_center",
    "com.apple.system.logger",
    "com.apple.logd",
    "com.apple.diagnosticd",
    "com.apple.SystemConfiguration.configd",
)

#: Added only when the policy trusts the job with the network: name resolution and TLS trust
#: evaluation. Never the keychain daemon.
_SEATBELT_NETWORK_MACH_SERVICES: tuple[str, ...] = (
    "com.apple.dnssd.service",
    "com.apple.mDNSResponder",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.ocspd",
)

#: Any path component spelled `.git`, in any letter case: macOS volumes are case-insensitive
#: by default, so `.GIT/config` IS `.git/config`. Matching the component rather than the
#: existing `.git` entries also covers a `.git` the job would CREATE (a new repository root,
#: or a `.git` file redirecting `gitdir:`), which VOOL's own git tools would then read.
_SEATBELT_GIT_METADATA_FILTER = '(regex #"/\\.[Gg][Ii][Tt]$" #"/\\.[Gg][Ii][Tt]/")'


def _trusted_sandbox_exec() -> str | None:
    """`_SANDBOX_EXEC` when it can be trusted to apply the profile, else None (and the job is
    refused). A symlink, a non-root owner, or a group/world-writable file or directory means
    someone other than the system can swap what runs, so it is not used."""
    path = Path(_SANDBOX_EXEC)
    try:
        binary = os.lstat(path)
        directory = os.lstat(path.parent)
    except OSError:
        return None
    if not stat.S_ISREG(binary.st_mode) or not stat.S_ISDIR(directory.st_mode):
        return None
    for entry in (binary, directory):
        if entry.st_uid != 0 or entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
    return str(path)


def _seatbelt_baseline(*, deny_network: bool) -> str:
    """The root-independent head of every job profile: deny by default, then only what a
    toolchain (python, pytest, node, npm, cargo, git) needs to start and run.

    It replaces `(allow default)`. That form denied a few things and allowed every operation
    nobody listed: Apple Events to other apps, signals to the operator's processes, keychain
    and LaunchServices lookups, preference reads through cfprefsd, and hard-link creation (a
    job could link an outside file into its workspace and then write through it).

    Processes may only signal and inspect processes inside the same sandbox. File READS stay
    open here and are narrowed by the layers `_macos_confined_profile` appends, which is the
    same read model as before; file WRITES are denied here and re-granted per root there.
    """
    services = _SEATBELT_TOOLCHAIN_MACH_SERVICES
    if not deny_network:
        services = (*services, *_SEATBELT_NETWORK_MACH_SERVICES)
    mach = " ".join(f'(global-name "{name}")' for name in services)
    return (
        "(version 1)"
        "(deny default)"
        '(import "system.sb")'
        "(allow process-fork)"
        "(allow process-exec*)"
        "(allow process-info* (target same-sandbox))"
        "(allow signal (target same-sandbox))"
        "(allow sysctl-read)"
        "(allow file-ioctl)"
        "(allow pseudo-tty)"
        "(allow ipc-posix-sem)"
        "(allow ipc-posix-shm)"
        "(allow system-socket)"
        f"(allow mach-lookup {mach})"
        "(allow file-read*)"
        f"{'(deny network*)' if deny_network else '(allow network*)'}"
    )


def _seatbelt_subpath_literal(path: Path) -> str:
    # Seatbelt string literals are double-quoted; escape embedded quotes/backslashes.
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'(subpath "{escaped}")'


def _private_read_roots() -> tuple[Path, ...]:
    """Private host trees hidden unless the policy names a specific read or write root.

    `/Volumes` joins the home and the temp trees: on macOS it is every mounted external
    volume — the operator's other disks — and a confined job has no business reading their
    contents by default. A volume the operator DOES name (a workspace or read root on an
    external disk) is re-allowed by the named-root layers on both backends: Seatbelt's
    last-match-wins allow clause follows the deny, and bwrap's --ro-bind/--bind restores the
    named path after the private tmpfs.
    """
    candidates = (
        Path.home(), Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"), Path("/Volumes"),
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates
                               if path.is_dir() and path.resolve() != Path(path.anchor)))


def _sensitive_read_deny_roots() -> list[Path]:
    """Directories that a sandboxed job must never read, even though reads are otherwise open so
    interpreters can load their stdlib. This closes the exfil target the audit named: an
    allowed interpreter (`python -c "open('~/.ssh/id_rsa').read()"`) reading credentials or
    VOOL's own key home at runtime, which the argv path-guard cannot see. Deny-listing these
    specific secret dirs is far safer than a read allow-list (which would break stdlib loading)."""
    home = Path.home()
    roots = [
        home / ".ssh", home / ".aws", home / ".gnupg", home / ".config" / "solana",
        home / ".config" / "gcloud", home / ".config" / "gh", home / ".config" / "solana-keygen",
        home / ".vool_runtime", home / ".config" / "solana" / "id.json",
    ]
    for env in ("VOOL_HOME", "VOOL_RUNTIME_HOME"):
        val = str(os.environ.get(env) or "").strip()
        if val:
            roots.append(Path(val))
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _runtime_protection_clauses(write_roots: tuple[Path, ...]) -> str:
    """Seatbelt clauses that keep this runtime's own state (no read, no write) and its running code (no write) out of a
    job's reach (core.runtime_state_protection). A write root strictly inside the code root that is an exempt workspace
    folder is allowed again; the code root itself never is."""
    try:
        from core.runtime_state_protection import code_write_exemptions, protected_code_roots, protected_state_roots

        state, code, exempt = protected_state_roots(), protected_code_roots(), code_write_exemptions()
    except Exception:
        return ""

    def literals(paths) -> str:
        return "".join(_seatbelt_subpath_literal(variant) for path in paths for variant in _path_variants(path))

    clauses = ""
    if code:
        clauses += f"(deny file-write* {literals(code)})"
        reallowed = [root for root in write_roots if any(root == allowed or allowed in root.parents for allowed in exempt)]
        if reallowed:
            clauses += f"(allow file-write* {literals(reallowed)})"
    if state:
        # file-read-data is named as well as file-read*: Seatbelt lets an earlier (allow file-read-data ...) for a containing
        # root win over a later (deny file-read* ...), measured with sandbox-exec; the explicit form placed last does not.
        clauses += f"(deny file-read* {literals(state)})(deny file-read-data {literals(state)})(deny file-write* {literals(state)})"
    return clauses


def _macos_confined_profile(
    allowed_roots: tuple[Path, ...],
    *,
    deny_network: bool = True,
    read_roots: tuple[Path, ...] = (),
) -> str:
    """Build a Seatbelt profile that confines file *writes* to the allowed workspace roots, and
    (when `deny_network` is true) also denies all network egress.

    It opens with `_seatbelt_baseline` (deny by default) and closes with a write deny on every
    `.git` path, so a job can read a repository's history but never plant a hook, a
    `core.fsmonitor` command or a `gitdir:` redirect that git would later run outside the
    sandbox, VOOL's own unsandboxed git tools included.

    Reads stay permitted so interpreters/tooling can load their stdlib and
    dylibs without bespoke allow-lists, EXCEPT for a deny-list of secret dirs
    (SSH/cloud/GPG credentials and VOOL's own key home) that the kernel blocks
    even from an allowed interpreter's runtime open() — the argv path-guard in
    :meth:`JobRunner.run` only sees literal path arguments, not runtime reads, so
    it is a first line, not the read boundary. Writes are denied by the kernel so
    a job cannot tamper with or persist outside its workspace even if the static
    guard is bypassed.

    ANVIL, 2026-08-07: `deny_network=False` exists because network trust and filesystem
    confinement are independent controls, not one boolean. `workspace.run_tests` (etc.) setting
    a trusted-local network relaxation used to skip building this profile AT ALL -- which meant a
    generated test that called `open('/tmp/x', 'w')` wrote there with no denial, no approval
    prompt, verified live (`test_macos_seatbelt_still_confines_writes_when_network_is_relaxed`).
    The write-confinement clauses below must never depend on whether network is also denied.
    """
    write_roots: list[Path] = []
    seen: set[str] = set()
    for root in allowed_roots:
        for variant in _path_variants(root):
            key = str(variant)
            if key not in seen:
                seen.add(key)
                write_roots.append(variant)
    allow_clauses = "".join(_seatbelt_subpath_literal(p) for p in write_roots)
    # /dev is needed for normal stdio (e.g. /dev/null, /dev/urandom).
    allow_clauses += '(subpath "/dev")'

    # READ confinement, in three layers, and the ORDER is the policy (Seatbelt is
    # last-match-wins):
    #
    #   1. `(deny file-read-data (subpath $HOME))` — the CONTENTS of the operator's home are
    #      unreadable. Deliberately `file-read-data` and not `file-read*`: the starred form
    #      also denies read-METADATA, and metadata on every ancestor directory is what a path
    #      lookup needs, so denying it makes `execvp()` of an interpreter living under the
    #      home fail with "Operation not permitted" before the job starts. Measured, not
    #      guessed. The cost is honest and stated in the docs: a job can still stat and list
    #      inside the home and learn that a file EXISTS. It cannot read a byte of it.
    #   2. `(allow file-read-data <read_roots + writable roots>)` — the named exceptions, and
    #      the reason `read_roots` exists at all: an interpreter has to load its own stdlib,
    #      and on this machine that stdlib lives under the home being denied.
    #   3. `(deny file-read* <secret roots>)` LAST, so it beats layer 2 even if a credential
    #      directory sits inside a read root. For these, metadata goes too: a job has no
    #      business learning the names of the keys in `~/.ssh` either.
    #
    # Denying the whole home rather than a list of secret directories is the point of the
    # change. A deny-list names the credential paths somebody thought of; browser profiles,
    # other worktrees, the operator's checkout, `~/.npmrc`, `~/.cargo/credentials` and a
    # downloads folder full of exports were on nobody's list, and a build script does not need
    # a path to be listed to read it.
    private_deny = "".join(_seatbelt_subpath_literal(v)
                           for root in _private_read_roots() for v in _path_variants(root))
    private_deny_clause = f"(deny file-read-data {private_deny})" if private_deny else ""
    read_back = "".join(
        _seatbelt_subpath_literal(variant)
        for root in (*read_roots, *allowed_roots)
        for variant in _path_variants(root)
    )
    read_back_clause = f"(allow file-read-data {read_back})" if read_back else ""
    secret_deny = "".join(
        _seatbelt_subpath_literal(variant)
        for root in _sensitive_read_deny_roots()
        for variant in _path_variants(root)
    )
    # Named file-read-data as well: a (deny file-read* ...) alone does NOT beat the earlier (allow file-read-data ...) of a read
    # root that contains a secret directory (measured with sandbox-exec, 2026-09-15); the explicit operation placed last does.
    secret_deny_clause = f"(deny file-read* {secret_deny})(deny file-read-data {secret_deny})" if secret_deny else ""
    # LAST, because Seatbelt is last-match-wins: this runtime's own state and running code stay out of reach even when an
    # allowed or read-back root contains them (core.runtime_state_protection).
    protection_clause = _runtime_protection_clauses(tuple(write_roots))
    return (
        f"{_seatbelt_baseline(deny_network=deny_network)}"
        "(deny file-write*)"
        f"(allow file-write* {allow_clauses})"
        f"{private_deny_clause}"
        f"{read_back_clause}"
        f"{secret_deny_clause}"
        f"{protection_clause}"
        # LAST of all: no root grant above, however broad, makes repository metadata writable.
        f"(deny file-write* {_SEATBELT_GIT_METADATA_FILTER})"
    )


def _path_variants(path: Path) -> tuple[Path, ...]:
    # macOS aliases /tmp -> /private/tmp and /var -> /private/var. The policy
    # stores the resolved path; include the unresolved alias too so a job
    # launched with the /tmp form is still allowed by the kernel profile.
    resolved = path.resolve()
    variants = [resolved]
    text = str(resolved)
    if text.startswith("/private/tmp"):
        variants.append(Path(text.replace("/private/tmp", "/tmp", 1)))
    elif text.startswith("/private/var"):
        variants.append(Path(text.replace("/private/var", "/var", 1)))
    return tuple(variants)


def _truncate(text: str, limit_kb: int) -> str:
    raw = text or ""
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit_kb * 1024:
        return raw
    return encoded[-(limit_kb * 1024) :].decode("utf-8", errors="replace")


def _decode_partial(captured: object) -> str:
    # subprocess.TimeoutExpired.stdout/stderr may be None, str (text=True), or
    # bytes depending on how far the read got; normalize to a string.
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return str(captured)


def _hard_linked_entry(roots: tuple[Path, ...]) -> tuple[Path | None, bool]:
    """``(offending_path, scan_complete)`` for the writable roots.

    Cheap in practice: 4,511 files in 0.02s on this repo, measured. `lstat` rather than
    `stat` so a symlink is never followed out of the root and counted for its target.
    """
    scanned = 0
    seen_roots: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen_roots or not root.exists():
            continue
        seen_roots.add(key)
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                scanned += 1
                if scanned > _HARDLINK_SCAN_FILE_LIMIT:
                    return None, False
                try:
                    stat_result = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if stat_result.st_nlink > 1:
                    return Path(dirpath) / name, True
    return None, True


def _terminate_process_group(process: subprocess.Popen, *, pgid: int | None = None) -> None:
    """Terminate all descendants remaining in the job's process group.

    The group id IS the child's pid because `run` starts it with `start_new_session=True`, so
    `run` tracks it from creation and passes it in: on the success-path sweep the direct child
    is already reaped, and `os.getpgid(process.pid)` on a reaped pid raises ProcessLookupError
    BEFORE the sweep — which made "no orphan survives" dead code there.

    Once a probe establishes the group is GONE, nothing further is ever signalled through that
    identifier: an integer process-group id is not a non-reusable identity, and the kernel may
    hand it to an unrelated process group the moment its last member is reaped. Escalation to
    SIGKILL happens only while the group is still demonstrably present at the end of the grace
    period; disappearance and inability-to-inspect both return without further signals.

    The remaining race, stated exactly: between any presence probe and the signal that follows
    it, the last member can exit and the id can be reused, so that one signal can land on an
    unrelated group. Linux's pidfd does not close this class: it is a handle to ONE process
    (that pid pinned against reuse only while the fd is open), not a whole-group lifetime
    guarantee — the group can outlive its leader, and group signalling still goes through the
    numeric id. macOS has no equivalent for process groups either, so the window is documented,
    not closed — and never widened (no name scans, no signalling of groups this job did not
    create). Detached descendants (their own `setsid()`) are outside group signalling entirely
    and remain confined only by the inherited kernel profile; no claim is made that every
    descendant is terminated.
    """
    if _is_windows_platform():  # pragma: no cover - no process groups of this shape on Windows
        with contextlib.suppress(Exception):
            process.kill()
        return
    target_pgid = pgid if pgid is not None else process.pid
    if target_pgid is None or target_pgid <= 0:
        return
    try:
        our_pgid = os.getpgid(0)
    except OSError:  # pragma: no cover - defensive: cannot learn our own group
        our_pgid = None
    if our_pgid is not None and target_pgid == our_pgid:  # pragma: no cover - never signal our own group
        return
    try:
        os.killpg(target_pgid, 0)
    except (OSError, ProcessLookupError):
        return  # the group is empty or uninspectable: nothing to terminate, nothing to signal
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(target_pgid, signal.SIGTERM)
    deadline = time.monotonic() + 3.0
    while True:
        try:
            os.killpg(target_pgid, 0)
        except (OSError, ProcessLookupError):
            # The group is gone. Its identifier is now free — everything after this point
            # would be signalling whatever the kernel gave the id to next, so stop here.
            return
        if time.monotonic() >= deadline:
            break  # still present after the grace period: escalate
        time.sleep(0.05)
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(target_pgid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.wait(timeout=5)


class JobRunner:
    def __init__(self, policy: ExecutionPolicy):
        self.policy = normalize_policy(policy)

    #: Variables a child genuinely needs to start. EVERYTHING else the operator's shell is
    #: carrying — API keys, cloud tokens, `SSH_AUTH_SOCK`, `GITHUB_TOKEN`, npm/cargo registry
    #: credentials, the operator's real `HOME` — is dropped. This is an allow-list on purpose:
    #: a deny-list of "secret-looking" names is the same losing game as scanning a command
    #: string, and the environment is the one channel where a build script can find a
    #: credential without ever naming a path.
    _ENV_PASSTHROUGH = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "SHELL",
        "USER",
        "LOGNAME",
    )

    def _child_environment(self, cwd_path: Path) -> dict[str, str]:
        """A minimal, sanitized environment whose writable roots are all inside the task root.

        `HOME`, `TMPDIR` and the XDG config/cache/data roots are pointed at directories under
        the job's own writable root. That matters twice over: a tool that writes to `~/.npm`,
        `~/.cargo` or `$TMPDIR` now writes inside the disposable area instead of being denied
        by the kernel and failing confusingly, and a tool that READS `$HOME/.aws/credentials`
        finds an empty directory rather than the operator's.
        """
        source = os.environ
        env = {
            key: str(source[key])
            for key in self._ENV_PASSTHROUGH
            if str(source.get(key) or "").strip()
        }
        task_root = Path(self.policy.workspace_root)
        home = task_root / ".sandbox-home"
        tmp = task_root / ".sandbox-tmp"
        for path in (home, tmp, home / ".cache", home / ".config", home / ".local" / "share"):
            with contextlib.suppress(OSError):
                path.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(tmp),
                "TEMP": str(tmp),
                "TMP": str(tmp),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "PWD": str(cwd_path),
                "VOOL_EXECUTION_BACKEND": self.policy.backend,
                # Proxies are an egress path that survives a network-denying profile on hosts
                # where the profile is not the boundary; refusing them costs nothing here.
                "NO_PROXY": "*",
                "no_proxy": "*",
                # A sandboxed command must not leave bytecode behind in a tree it merely READ.
                # `python -m unittest` importing a subject module writes `__pycache__/<stem>.pyc`
                # next to it, which for a read-only audit is a real, CI-observed mutation of a
                # tree asserted byte-identical (run 31131255470).
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if _is_windows_platform():
            # A Windows child needs SYSTEMROOT to start at all: without it Python dies in
            # `_Py_HashRandomization_Init` because it can't load the system crypto provider.
            # WINDIR and PATHEXT are the same kind of system fact, not operator secrets. The
            # per-user folders are pointed into the sandbox home, like HOME on POSIX.
            for key in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
                value = str(source.get(key) or "").strip()
                if value:
                    env[key] = value
            roaming, local = home / "AppData" / "Roaming", home / "AppData" / "Local"
            for path in (roaming, local):
                with contextlib.suppress(OSError):
                    path.mkdir(parents=True, exist_ok=True)
            env.update({"USERPROFILE": str(home), "APPDATA": str(roaming), "LOCALAPPDATA": str(local)})
        return env

    #: Returned when the operator cancelled the job. Distinct from 124 (timed out) so a
    #: receipt can say which happened; 125 is unused by the shells this runs.
    CANCELLED_RETURNCODE = 125

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ExecutionResult:
        if not argv:
            raise ValueError("No command provided.")
        if cwd is None:
            cwd = self.policy.workspace_root
        cwd_path = Path(cwd).resolve()
        allowed_roots = (self.policy.workspace_root, *tuple(self.policy.writable_roots))
        if not path_within_roots(cwd_path, allowed_roots):
            raise ValueError("Execution cwd escapes allowed workspace roots.")
        escaping = path_args_within_roots(argv, allowed_roots, cwd=cwd_path)
        if escaping is not None:
            raise ValueError(f"Path argument '{escaping}' escapes allowed workspace roots.")
        if command_uses_network(argv) and not self.policy.allow_network_egress:
            raise ValueError("Network egress is disabled by execution policy.")
        linked, scan_complete = _hard_linked_entry(allowed_roots)
        if linked is not None:
            raise HardLinkedWritableRootError(
                f"'{linked}' has more than one name on this filesystem. A hard link is a second "
                "path to the same file, and path-based kernel confinement cannot tell them "
                "apart, so a write inside the workspace could land outside it."
            )
        if not scan_complete:
            raise HardLinkedWritableRootError(
                f"the writable roots hold more than {_HARDLINK_SCAN_FILE_LIMIT} files, so they "
                "could not be checked for hard links. Refusing rather than reporting a "
                "confinement that was not verified."
            )
        argv = self._with_network_isolation(argv, allowed_roots)
        with _WINDOWS_STAGED_LOCK:
            staged = _WINDOWS_STAGED.pop(argv[2], None) if len(argv) > 3 and argv[1] == "--policy" else None
        if staged is None:
            return self._run_wrapped(argv, cwd_path, cancel_event)
        stage, grant_key = staged
        try:
            return self._run_windows_confined(argv, cwd_path, cancel_event, stage, grant_key, allowed_roots)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _run_windows_confined(
        self,
        argv: list[str],
        cwd_path: Path,
        cancel_event: threading.Event | None,
        stage: Path,
        grant_key: str,
        allowed_roots: tuple[Path, ...],
    ) -> ExecutionResult:
        """Run the VoolConfine-wrapped job, then trust nothing the launcher did not record.

        The child shares the launcher's exit code space and stderr, so a 125 or a
        `VOOLCONFINE-REFUSED:` line could be the child's own. The status file sits outside every
        writable root, where the child can't write, and it alone says whether the child ran.
        No record, or `child_started: false`, means the job never ran confined: it is refused
        (`KernelIsolationUnavailableError`), never reported as a command failure. A repository
        layout the job created (exit 123, or `new_repositories` in the record) refuses the result
        whatever else happened (`NestedGitMetadataError`).

        The launcher's own repository scan runs only when the job ends by itself. A timeout or a
        cancel kills the launcher first, and a crashed launcher never scans. So the runner also
        scans the write roots before the job and again after it, on EVERY outcome: timeout,
        cancel, refusal and exception included. A new layout refuses the result.
        """
        roots = tuple(dict.fromkeys(Path(root).resolve() for root in allowed_roots))
        layouts_before, complete = _repository_layouts(roots)
        if not complete:
            raise HardLinkedWritableRootError(
                f"the writable roots hold more than {_HARDLINK_SCAN_FILE_LIMIT} entries, so they "
                "could not be checked for repository layouts. Refusing rather than reporting a "
                "confinement that was not verified."
            )
        try:
            return self._run_windows_launcher(argv, cwd_path, cancel_event, stage, grant_key)
        finally:
            # Deliberately raises from `finally`: a planted repository outranks whatever the run
            # returned or raised, including a timeout or a cancel.
            layouts_after, complete = _repository_layouts(roots)
            planted = sorted(layouts_after - layouts_before)
            if planted or not complete:
                where = planted[0] if planted else "the writable roots (scan cap reached)"
                raise NestedGitMetadataError(
                    f"the job created git metadata at '{where}'. Git would run hooks and config from "
                    "it outside the sandbox, so this result is refused. Nothing was removed."
                )

    def _run_windows_launcher(
        self,
        argv: list[str],
        cwd_path: Path,
        cancel_event: threading.Event | None,
        stage: Path,
        grant_key: str,
    ) -> ExecutionResult:
        from sandbox import windows_confinement

        def adopt(process: subprocess.Popen) -> None:
            try:
                windows_confinement.adopt_launcher(process.pid)
            except Exception as exc:
                raise KernelIsolationUnavailableError(
                    f"the Windows confinement launcher could not be put in VOOL's own job ({exc}), "
                    "so it was stopped before it ran anything."
                ) from None

        outcome: dict[str, str] = {}
        result = self._run_wrapped(
            argv,
            cwd_path,
            cancel_event,
            creationflags=windows_confinement.launcher_creationflags(),
            outcome=outcome,
            env_filter=windows_confinement.launcher_environment,
            after_spawn=adopt,
        )
        # The cancel and timeout outcomes are the runner's own: it killed the launcher, so the
        # job object closed and the tree is gone, whatever the status file says.
        operator_outcome = outcome.get("kind") in {"cancelled", "timed_out"}
        status = windows_confinement.read_status(stage / "status.json")
        child_never_ran = status is not None and not status.child_started
        if not child_never_ran and (status is None or not status.repositories_scanned):
            # The launcher never finished its own repository scan: the runner killed it (timeout,
            # cancel), or it died. An empty `new_repositories` then proves nothing, so the scan
            # form runs before anything is trusted or returned, even a timeout or a cancel. (A
            # missing record counts as "may have run": the launcher writes child_started only
            # after the resume, so a crash in between leaves no record of a child that ran.)
            scan_rc = windows_confinement.launcher_scan(argv[0], Path(argv[2]))
            rescanned = windows_confinement.read_status(stage / "status.json")
            if scan_rc == windows_confinement.EXIT_NEW_REPOSITORY or (
                rescanned is not None and rescanned.new_repositories
            ):
                where = rescanned.new_repositories[0] if rescanned and rescanned.new_repositories else "a write root"
                raise NestedGitMetadataError(
                    f"the job created git metadata at '{where}'. Git would run hooks and config "
                    "from it outside the sandbox, so this result is refused. Nothing was removed."
                )
            if scan_rc != 0 or rescanned is None or not rescanned.repositories_scanned:
                raise KernelIsolationUnavailableError(
                    "Windows confinement could not finish the repository scan after this job "
                    f"(scan exit {scan_rc}), so its result is refused: a repository it planted "
                    "could not be ruled out."
                )
        if status is not None and status.new_repositories:
            raise NestedGitMetadataError(
                f"the job created git metadata at '{status.new_repositories[0]}'. Git would run "
                "hooks and config from it outside the sandbox, so this result is refused. "
                "Nothing was removed."
            )
        if result.returncode == windows_confinement.EXIT_NEW_REPOSITORY and not operator_outcome:
            raise NestedGitMetadataError(
                "the launcher exited 123 (a repository layout appeared under a writable root), so "
                "this result is refused, whatever the job itself returned. Nothing was removed."
            )
        if not operator_outcome:
            if status is None or not status.child_started:
                with _WINDOWS_STAGED_LOCK:
                    _WINDOWS_GRANTED.discard(grant_key)  # a verify miss: grant again next time
                reason = (status.error if status is not None else None) or "no launcher status record"
                raise KernelIsolationUnavailableError(
                    "Windows confinement could not be set up, so the job never ran "
                    f"(launcher exit {result.returncode}: {reason})."
                )
            if status.exit_code is None or status.exit_code != result.returncode:
                if not (status.timed_out and result.returncode == windows_confinement.EXIT_TIMEOUT):
                    raise KernelIsolationUnavailableError(
                        "Windows confinement's launcher record does not match its exit code "
                        f"(record {status.exit_code}, exit {result.returncode}); the result is not trusted."
                    )
            if status.limit in ("memory", "processes") and result.returncode == 0:
                # A Windows job limit fails the allocation or the process start instead of ending
                # the process, so a child that catches the error can still exit 0. The job went
                # over its limit: it did not succeed, as a cgroup OOM kill would show on Linux.
                what = (
                    "its memory limit and was stopped from allocating more"
                    if status.limit == "memory"
                    else "its process limit and was stopped from starting more"
                )
                return ExecutionResult(
                    returncode=1,
                    stdout=result.stdout,
                    stderr=result.stderr + f"\nThe job went over {what}.\n",
                )
        return result

    def _run_wrapped(
        self,
        argv: list[str],
        cwd_path: Path,
        cancel_event: threading.Event | None,
        *,
        creationflags: int = 0,
        outcome: dict[str, str] | None = None,
        env_filter=None,
        after_spawn=None,
    ) -> ExecutionResult:
        if outcome is None:
            outcome = {}
        env = self._child_environment(cwd_path)
        if env_filter is not None:
            env = env_filter(env)
        # `start_new_session=True` puts the child in its OWN process group, which is what makes
        # the whole tree killable. `subprocess.run(timeout=…)` kills the direct child and reaps
        # it; anything that child spawned keeps running, reparented to init, with the caller
        # told the job "timed out and was terminated". A build script that backgrounds a daemon
        # or a watcher — exactly what an npm lifecycle hook does — outlived every job here.
        process = subprocess.Popen(
            argv,
            cwd=str(cwd_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=env,
            start_new_session=True,
            creationflags=creationflags,
        )
        # The group id IS the child's pid (start_new_session), captured at creation: the child
        # may be reaped by the time any teardown runs, and the tracked value is the identifier
        # every teardown probes and signals.
        pgid = process.pid
        teardown_done = False

        def teardown() -> None:
            """Own the group teardown exactly ONCE per run.

            Every path — cancel, timeout, caller exception, normal return — goes through this
            one door. The except arms below must tear down before they drain the pipes, and the
            finally arm sweeps the success path; without the once-flag those two would BOTH run,
            and the second teardown would re-signal an identifier the first already proved gone
            — with the kernel free to have handed that id to an unrelated process group.
            """
            nonlocal teardown_done
            if teardown_done:
                return
            teardown_done = True
            _terminate_process_group(process, pgid=pgid)

        try:
            if after_spawn is not None:
                after_spawn(process)
            stdout, stderr = self._wait(process, cancel_event)
        except _JobCancelledError:
            # The operator withdrew the job. Same teardown as a timeout — the whole process
            # group, children and grandchildren — and a distinct code so the receipt can say
            # "cancelled" rather than "failed".
            outcome["kind"] = "cancelled"
            teardown()
            partial_out, partial_err = "", ""
            with contextlib.suppress(Exception):
                partial_out, partial_err = process.communicate(timeout=5)
            note = "Execution was cancelled by the operator and the process group was terminated."
            return ExecutionResult(
                returncode=self.CANCELLED_RETURNCODE,
                stdout=_truncate(partial_out or "", self.policy.max_output_kb),
                stderr=_truncate(f"{partial_err}\n{note}".strip(), self.policy.max_output_kb),
            )
        except subprocess.TimeoutExpired as exc:
            outcome["kind"] = "timed_out"
            teardown()
            partial_stdout = _decode_partial(exc.stdout)
            partial_stderr = _decode_partial(exc.stderr)
            with contextlib.suppress(Exception):
                # After the group is dead the pipes drain without blocking, and whatever the
                # job managed to say before it was killed is the most useful thing in the
                # result. `communicate` on a reaped process returns immediately.
                drained_out, drained_err = process.communicate(timeout=5)
                partial_stdout = partial_stdout or (drained_out or "")
                partial_stderr = partial_stderr or (drained_err or "")
            timeout_note = f"Execution timed out after {self.policy.max_seconds}s and was terminated."
            combined_stderr = f"{partial_stderr}\n{timeout_note}".strip() if partial_stderr else timeout_note
            return ExecutionResult(
                returncode=124,
                stdout=_truncate(partial_stdout, self.policy.max_output_kb),
                stderr=_truncate(combined_stderr, self.policy.max_output_kb),
            )
        except BaseException:
            # Cancellation (KeyboardInterrupt), a caller thread dying, anything at all: the
            # process group does not outlive the call that started it.
            teardown()
            raise
        finally:
            # A job that returned normally can still have left the group populated — a
            # backgrounded grandchild does not keep the parent alive. This arm sweeps the
            # success path (the except arms already owned their teardown through the same
            # once-only door), so "no orphan survives" holds on every path without any path
            # signalling the group identifier twice.
            teardown()
        return ExecutionResult(
            returncode=int(process.returncode),
            stdout=_truncate(stdout, self.policy.max_output_kb),
            stderr=_truncate(stderr, self.policy.max_output_kb),
        )

    def _wait(self, process: subprocess.Popen, cancel_event) -> tuple[str, str]:
        """Wait for the job, honouring cancellation as promptly as the timeout.

        Without a cancel event this is `communicate(timeout=…)` unchanged. With one, the wait
        is sliced so a cancellation lands in well under a second instead of whenever the job
        happens to finish — a cancel that is only noticed at completion is not a cancel.
        """
        if cancel_event is None:
            return process.communicate(timeout=self.policy.max_seconds)
        deadline = time.monotonic() + float(self.policy.max_seconds)
        while True:
            if cancel_event.is_set():
                raise _JobCancelledError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, self.policy.max_seconds)
            try:
                return process.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue

    def _with_network_isolation(
        self, argv: list[str], allowed_roots: tuple[Path, ...] = ()
    ) -> list[str]:
        """Wrap ``argv`` in this host's kernel confinement. NEVER returns it unwrapped.

        The name is historical and the behaviour is not: this is the only place a confinement
        profile is ever applied, so whatever it returns is the entire boundary around a child
        process. It used to open with

            if self.policy.allow_network_egress:
                return argv

        which made "we trust this command with the network" mean "run it with no profile at
        all" — no write confinement, no read denial, no secret-directory rule. Every base
        command in `SandboxRunner._PACKAGE_INSTALL_COMMANDS` took that path, so `cargo build`
        (build.rs, proc macros), `npm run <script>` and `pip uninstall` executed arbitrary
        third-party code against the operator's home with nothing but a scan of their argv
        tokens in front of them.

        Network egress and filesystem confinement are independent controls. Trusting one has
        never been a reason to drop the other, and a host that cannot apply a profile refuses
        the job (`KernelIsolationUnavailableError`) rather than running it raw and calling it
        sandboxed.
        """
        if not allowed_roots:
            allowed_roots = (self.policy.workspace_root, *tuple(self.policy.writable_roots))
        mode = (self.policy.network_isolation_mode or "auto").strip().lower()
        if mode not in {"auto", "os_enforced", "heuristic_only"}:
            mode = "auto"
        if self.policy.allow_network_egress or mode == "heuristic_only":
            # ANVIL, 2026-08-07: this used to `return argv` unmodified -- skip the wrapper
            # entirely. On macOS and Linux+bwrap, the SAME wrapper that denies network is what
            # denies out-of-workspace file writes, so relaxing network trust for a local
            # pytest/ruff run silently dropped filesystem confinement too: a generated test
            # calling `open('/tmp/x', 'w')` wrote there with no denial and no approval prompt
            # (`sandbox.run_command`, which never gets this relaxation, correctly denied the
            # identical write). Filesystem confinement must hold regardless of network trust, so
            # this still applies whatever real backend can express "confine writes, allow
            # network" independently. It no longer falls back to the bare static argv-guard:
            # that fallback WAS the hole, and a guard that reads argument strings is not a
            # sandbox and must never be reported as one.
            confined = self._filesystem_only_isolation_prefix(argv, allowed_roots)
            if confined is not None:
                return confined
            # No backend that can express "confine writes, permit network" on this host. The
            # previous behaviour here was `return argv` — the static-guard fallback — and that
            # is precisely the shape this lane exists to delete: running raw while the caller
            # and every receipt still say "sandboxed". Refuse instead.
            raise KernelIsolationUnavailableError(self._no_kernel_isolation_message())
        isolated = self._kernel_network_isolation_prefix(argv, allowed_roots)
        if isolated is not None:
            return isolated
        # No OS-enforced backend on this host, in ANY mode. macOS has sandbox-exec and Linux
        # has bwrap, so reaching here means no kernel enforcement is possible
        # — and there is no longer a mode that answers that by running the job anyway.
        # `heuristic_only` now relaxes NETWORK trust only; it never relaxes confinement, and on
        # a host with no backend it refuses exactly like the other two.
        raise KernelIsolationUnavailableError(self._no_kernel_isolation_message())

    def _no_kernel_isolation_message(self) -> str:
        base = (
            "OS-level filesystem confinement and network isolation are required but unavailable "
            "(expected bwrap on Linux, sandbox-exec on macOS, the pinned VoolConfine launcher on Windows). "
            "A backend may be installed and still unusable: containers and CI runners commonly "
            "ship util-linux but deny the namespace syscall, so each backend is probed for whether "
            "it actually runs, not merely for whether it is on PATH."
        )
        if _is_windows_platform():
            # Windows confines only through the pinned VoolConfine launcher (AppContainer + Job
            # Object). Reaching here means it is missing, fails its pin or failed its probe, so the
            # job fails closed. Name the two real options instead of leaving the operator guessing.
            return (
                f"{base} On Windows that is the pinned VoolConfine launcher ({{app}}\\broker\\confine, "
                "or windows\\broker\\publish\\confine in a checkout, checked against its pin); it is "
                "missing, does not match its pin, or failed its probe, so no executable tool runs here. "
                "Reinstall VOOL to restore it, or run VOOL under WSL2/Linux so bwrap provides "
                "kernel-enforced confinement. There is deliberately no override — the only thing "
                "an override could buy is running the job with no boundary while still calling "
                "it sandboxed."
            )
        return (
            f"{base} There is no override: `network_isolation_mode='heuristic_only'` relaxes "
            "NETWORK trust only and still requires a real backend for filesystem confinement."
        )

    def _kernel_network_isolation_prefix(
        self, argv: list[str], allowed_roots: tuple[Path, ...]
    ) -> list[str] | None:
        # Windows: AppContainer + Job Object through the pinned VoolConfine launcher, and only
        # that. A Windows host never falls through to the Linux or macOS backends.
        if _is_windows_platform():
            return self._windows_confine_prefix(argv, allowed_roots)
        # Prefer hardened Linux isolation backends when present.
        isolated = self._linux_bwrap_prefix(argv, allowed_roots)
        if isolated is not None:
            return isolated
        # Network-only wrappers cannot enforce the filesystem contract.
        # macOS: real kernel-enforced network denial via Seatbelt (sandbox-exec).
        return self._macos_sandbox_exec_prefix(argv, allowed_roots)

    def _filesystem_only_isolation_prefix(
        self, argv: list[str], allowed_roots: tuple[Path, ...]
    ) -> list[str] | None:
        """Confine filesystem writes to `allowed_roots` WITHOUT denying network egress.

        `unshare -n` and firejail's `--net=none` only ever express network isolation, so they
        offer nothing here -- only bwrap (`--ro-bind / /` plus selective `--bind`) and macOS
        Seatbelt can express "confine writes, permit network" independently. `None` means neither
        backend exists on this host; the caller refuses the command. A Windows host uses the
        VoolConfine launcher only.
        """
        if _is_windows_platform():
            return self._windows_confine_prefix(argv, allowed_roots, deny_network=False)
        isolated = self._linux_bwrap_prefix(argv, allowed_roots, deny_network=False)
        if isolated is not None:
            return isolated
        return self._macos_sandbox_exec_prefix(argv, allowed_roots, deny_network=False)

    def _windows_confine_prefix(
        self, argv: list[str], allowed_roots: tuple[Path, ...], *, deny_network: bool = True
    ) -> list[str] | None:
        """`[VoolConfine.exe, --policy, <json>, --, *argv]`, or None when the backend is unproven.

        docs/WINDOWS_CONFINEMENT.md is the contract. `None` makes `_with_network_isolation`
        refuse with its unchanged base message. A program phase 1 does not support is refused
        here with its own message, because "this host cannot confine" would send the operator
        looking for the wrong fix.
        """
        if not _is_windows_platform():
            return None
        from sandbox import windows_confinement

        project_root = _windows_project_root()
        launcher = windows_confinement.launcher_path(project_root)
        if launcher is None:
            return None
        probe_key = _windows_probe_key(launcher)
        usable = _BACKEND_USABLE.get(probe_key)
        if usable is None:
            usable = bool(windows_confinement.probe(project_root))
            _BACKEND_USABLE[probe_key] = usable
        if not usable:
            return None
        program = self._windows_phase1_argv(argv)
        policy, stage = self._windows_policy(allowed_roots, deny_network=deny_network)
        if policy is None or stage is None:
            return None
        policy_path = stage / "policy.json"
        try:
            prefix = windows_confinement.confinement_prefix(
                program, policy, project_root=project_root, policy_path=policy_path
            )
        except ValueError:
            prefix = None
        if prefix is None:
            shutil.rmtree(stage, ignore_errors=True)
            return None
        # The run form only verifies ACLs. VOOL grants them first, unconfined, with the same
        # policy. A grant that doesn't exit 0 refuses the job.
        grant_key = json.dumps(
            {
                key: value
                for key, value in json.loads(policy.to_json()).items()
                if key in {"write_roots", "read_roots", "deny_write_paths", "deny_all_paths", "deny_write_git"}
            },
            sort_keys=True,
        )
        with _WINDOWS_STAGED_LOCK:
            granted = grant_key in _WINDOWS_GRANTED
        if not granted:
            if not windows_confinement.launcher_form(prefix[0], "grant", policy_path):
                shutil.rmtree(stage, ignore_errors=True)
                raise KernelIsolationUnavailableError(
                    "Windows confinement could not grant this job's folders (the launcher's grant "
                    "step failed), so the job was not run."
                )
            with _WINDOWS_STAGED_LOCK:
                _WINDOWS_GRANTED.add(grant_key)
        with _WINDOWS_STAGED_LOCK:
            _WINDOWS_STAGED[prefix[2]] = (stage, grant_key)
        return prefix

    @staticmethod
    def _windows_phase1_argv(argv: list[str]) -> list[str]:
        """The job's argv with its program pinned to the interpreter running VOOL, or a refusal.

        `python`/`python3`/`py` and `pytest` become this interpreter (`pytest` as `-m pytest`).
        On Windows a bare `python3` resolves to the Microsoft Store alias stub, and the
        launcher never searches PATH anyway. An absolute program is accepted only when it IS
        this interpreter or its base. Nothing else runs in phase 1.

        A Python command line must also be a `.py` script, `-c <code>` or `-m pytest`, `-m unittest`
        or `-m compileall` (`_windows_python_args_refusal`). Judging only the program name let `python -m pip`,
        `-m ensurepip`, `-m venv` and `-m ruff` through.
        """
        interpreter = Path(sys.executable).resolve()
        accepted = {os.path.normcase(str(interpreter))}
        base = getattr(sys, "_base_executable", "") or ""
        if base:
            accepted.add(os.path.normcase(str(Path(base).resolve())))
        program = str(argv[0]) if argv else ""
        name = Path(program.replace("\\", "/")).name.lower()
        stem = name[:-4] if name.endswith(".exe") else name
        rest = [str(item) for item in argv[1:]]
        python: list[str] | None = None
        if "/" not in program and "\\" not in program:  # a bare name, not a path (any case)
            if stem in _WINDOWS_PYTHON_NAMES:
                python = [str(interpreter), *rest]
            elif stem in _WINDOWS_PYTEST_NAMES:
                return [str(interpreter), "-m", "pytest", *rest]
        elif Path(program).is_absolute() and os.path.normcase(str(Path(program).resolve())) in accepted:
            python = [str(Path(program).resolve()), *rest]
        if python is not None:
            refused = _windows_python_args_refusal(rest)
            if refused is None:
                return python
            raise KernelIsolationUnavailableError(
                f"Windows confinement runs Python and pytest jobs only for now: a Python job must be "
                f"a .py script, -c code or -m pytest (or VOOL's own -m unittest and -m compileall), "
                f"so {refused} is refused. VOOL 0.7.1 on "
                "Windows refuses pip, ruff, venv and other Python modules. Run them under "
                "WSL2/Linux instead. It was not run."
            )
        raise KernelIsolationUnavailableError(
            f"Windows confinement runs Python and pytest jobs only for now; '{program}' is refused "
            "until its toolchain is proven inside the confinement (git, npm, node, cargo, cmd and "
            "PowerShell included). It was not run."
        )

    def _windows_policy(self, allowed_roots: tuple[Path, ...], *, deny_network: bool):
        """The launcher policy for this job and the staging folder that holds its files.

        The staging folder is a fresh `mkdtemp` that must sit outside every writable root, so
        the child can't rewrite its own rules or forge the launcher's status record. A layout
        the Windows ACL model can't express the way the Mac profile does is refused
        (None, None), never approximated:
        - a write root inside VOOL's code without the Mac's exemption, where an explicit allow ACE
          would beat the inherited deny;
        - a read or write root that contains VOOL's state or a credential folder.
        """
        from sandbox import windows_confinement

        write_roots = tuple(dict.fromkeys(Path(root).resolve() for root in allowed_roots))
        workspace = Path(self.policy.workspace_root).resolve()
        # Shared: only the interpreter and its base, readable by every confined job. The workspace
        # and the policy's read roots are this job's own: each gets a read capability only a job
        # naming it carries, so a grant left on disk never lets another workspace's job read it
        # (the Mac profile likewise allows reads of this job's roots only).
        shared_read_roots = tuple(
            dict.fromkeys(Path(root) for root in windows_confinement.interpreter_read_roots())
        )
        job_read_roots = tuple(
            dict.fromkeys(
                [
                    *(Path(root).resolve() for root in self.policy.read_roots),
                    workspace,
                ]
            )
        )
        read_roots = (*shared_read_roots, *job_read_roots)
        try:
            from core.runtime_state_protection import (
                code_write_exemptions,
                protected_code_roots,
                protected_state_roots,
            )

            state = tuple(Path(p).resolve() for p in protected_state_roots())
            code = tuple(Path(p).resolve() for p in protected_code_roots())
            exempt = tuple(Path(p).resolve() for p in code_write_exemptions())
        except Exception:
            return None, None  # the protected set is unknown: fail closed

        def within(path: Path, root: Path) -> bool:
            return path == root or root in path.parents

        for root in write_roots:
            if any(within(root, c) for c in code) and not any(within(root, e) for e in exempt):
                return None, None
        sensitive = (*state, *(Path(p).resolve() for p in _sensitive_read_deny_roots()))
        for root in (*write_roots, *read_roots):
            if any(within(secret, root) for secret in sensitive):
                return None, None
            if any(within(root, protected) for protected in state):
                return None, None  # VOOL's state is no-read, so no root may sit inside it
        stage = Path(tempfile.mkdtemp(prefix="vool-confine-")).resolve()
        if any(within(stage, root) for root in write_roots):
            shutil.rmtree(stage, ignore_errors=True)
            return None, None
        temp_dir = workspace / ".sandbox-tmp"
        with contextlib.suppress(OSError):
            temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            policy = windows_confinement.WindowsConfinementPolicy(
                write_roots=tuple(_windows_path_text(root) for root in write_roots),
                read_roots=tuple(_windows_path_text(root) for root in shared_read_roots),
                job_read_roots=tuple(_windows_path_text(root) for root in job_read_roots),
                # A read root inside a write root would stay writable through the root's grant
                # (a workspace .venv), so it is denied writes as well.
                deny_write_paths=tuple(
                    dict.fromkeys(
                        _windows_path_text(root)
                        for root in (
                            *code,
                            *(
                                r for r in read_roots
                                if r not in write_roots and any(within(r, w) for w in write_roots)
                            ),
                        )
                    )
                ),
                # Like the Mac's secret and state denies: no read, no write. Only folders that
                # exist are named; a job can't create one, because nothing outside its write roots
                # is granted to it.
                deny_all_paths=tuple(
                    dict.fromkeys(_windows_path_text(root) for root in sensitive if Path(root).exists())
                ),
                deny_write_git=True,
                network=not deny_network,
                # A backstop only: the runner's own clock ends the job first and kills the launcher.
                max_seconds=int(self.policy.max_seconds) + 5,
                memory_mb=int(self.policy.max_memory_mb),
                temp_dir=_windows_path_text(temp_dir),
                status_path=_windows_path_text(stage / "status.json"),
            )
            policy.validate()
        except ValueError:
            shutil.rmtree(stage, ignore_errors=True)
            return None, None
        return policy, stage

    def _macos_sandbox_exec_prefix(
        self, argv: list[str], allowed_roots: tuple[Path, ...], *, deny_network: bool = True
    ) -> list[str] | None:
        if sys.platform != "darwin":
            return None
        sandbox_exec = _trusted_sandbox_exec()
        if not sandbox_exec:
            return None
        # The root-independent baseline every real profile starts with: does Seatbelt run here
        # at all, AND does this macOS accept every operation the baseline names. An older
        # release that rejects one fails the probe and the job is refused, never run with a
        # weaker profile. Probing with the real profile would key a cached verdict to one
        # call's roots. The probe runs bare `true`, like the bwrap probe below: callers that
        # intercept process launches recognise a backend probe by that argv.
        if not _backend_usable(
            "sandbox-exec",
            [sandbox_exec, "-p", _seatbelt_baseline(deny_network=True), "--", "true"],
        ):
            return None
        # An explicitly selected executable may live in a private tool directory.
        # Grant that file, never arbitrary path arguments or its unrelated siblings.
        executable = shutil.which(argv[0]) if argv else None
        program_reads = (Path(executable).resolve(),) if executable else ()
        profile = _macos_confined_profile(
            allowed_roots, deny_network=deny_network,
            read_roots=(*tuple(self.policy.read_roots or ()), *program_reads)
        )
        return [sandbox_exec, "-p", profile, "--", *list(argv)]

    def _linux_bwrap_prefix(
        self, argv: list[str], allowed_roots: tuple[Path, ...], *, deny_network: bool = True
    ) -> list[str] | None:
        if os.name != "posix":
            return None
        if not sys.platform.startswith("linux"):
            return None
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return None
        net_flag = ["--unshare-net"] if deny_network else []
        # Probed without the per-root binds: the question is whether this host grants the
        # namespace at all, not whether one particular root exists. Keyed separately per
        # `deny_network` -- with the flag dropped, this probes plain bwrap sandboxing (no network
        # namespace required), a different capability than the deny_network=True probe.
        if not _backend_usable(
            f"bwrap:deny_network={deny_network}",
            [bwrap, *net_flag, "--ro-bind", "/", "/", "--tmpfs", "/tmp", "--", "true"],
        ):
            return None
        # FS isolation in addition to (optionally) the network namespace: mount the whole
        # host read-only, then re-bind only the allowed workspace roots writable,
        # plus a private tmpfs for /tmp. 'unshare -n' alone gives NO filesystem
        # isolation, so bwrap is the preferred backend when present. The `--ro-bind`/`--bind`
        # confinement below applies regardless of `deny_network` -- ANVIL, 2026-08-07: filesystem
        # confinement must hold even when network trust is relaxed for a local test/lint run.
        cmd: list[str] = [bwrap, *net_flag, "--ro-bind", "/", "/", "--tmpfs", "/tmp"]
        # The whole-root read-only bind takes /dev with it, and a read-only mount refuses
        # O_RDWR on its files -- so /dev/null became unopenable and every child that opens
        # os.devnull at startup died with PermissionError before doing any work (measured
        # on the CI runners 2026-09-21: a sandboxed `python -m pytest` crashed inside
        # capman.start_global_capturing -> FDCapture, which opens os.devnull for stdin,
        # leaving every journaled narrow/cumulative step at rc=1 "command_failed" while
        # the same steps ran green under the macOS Seatbelt prefix, whose read-only roots
        # do not cover /dev). A stacked writable --bind of the device node still refuses
        # the open inside the user namespace (measured same day), so the sandbox gets a
        # FRESH dev tmpfs via --dev: bwrap creates the standard character devices itself,
        # which is the canonical way and gives children the null/zero/random/urandom a
        # toolchain opens without lending them any of the host's device nodes.
        cmd += ["--dev", "/dev"]
        # /tmp is private above; host home and other temp trees are private too.
        # Restore the policy's declared read roots BEFORE writable roots. Without these
        # read binds a read-only plugin in /tmp cannot even execute its own handler.
        private_roots = tuple(dict.fromkeys((Path("/tmp"), *_private_read_roots())))
        for root in private_roots:
            if root != Path("/tmp"):
                cmd += ["--tmpfs", str(root)]
        executable = shutil.which(argv[0]) if argv else None
        program_reads = (Path(executable).resolve(),) if executable else ()
        for root in dict.fromkeys((*self.policy.read_roots, self.policy.workspace_root, *program_reads)):
            resolved = Path(root).resolve()
            if resolved.exists():
                cmd += ["--ro-bind", str(resolved), str(resolved)]
        for root in allowed_roots:
            resolved = str(root.resolve() if isinstance(root, Path) else Path(root).resolve())
            cmd += ["--bind", resolved, resolved]
        # Mountpoint parents created inside the private tmpfs trees are otherwise
        # writable. A child could report an unauthorized write there even though
        # the file disappears with its namespace. Freeze those trees after all
        # mountpoints exist; separately mounted write grants remain writable.
        write_paths = tuple(Path(root).resolve() for root in allowed_roots)
        for root in private_roots:
            if not any(root == allowed or root.is_relative_to(allowed) for allowed in write_paths):
                cmd += ["--remount-ro", str(root)]
        cmd += ["--", *list(argv)]
        return cmd


#: The plain answer when phase 1 does not run a program at all, given before any approval is asked
#: or any model is called (`windows_program_refusal`). `{program}` is the program as the user typed it.
WINDOWS_PROGRAM_REFUSED_TEXT = (
    "On Windows, VOOL 0.7.1 runs only Python and pytest in its sandbox; it doesn't run {program}. "
    "Use WSL2/Linux for that. Nothing was run."
)


def windows_program_refusal(argv: list[str] | tuple[str, ...]) -> str | None:
    """On Windows, why phase 1 would refuse to run `argv`; None when it would run it, and always off
    Windows.

    Asked BEFORE the permission decision and before a model is called, so a program that can never
    run gets a plain refusal instead of an approval card. The decision is `JobRunner._windows_phase1_argv`
    itself, the gate `_windows_confine_prefix` applies when the job runs, so there is one list of
    what phase 1 runs. When the program is one phase 1 runs (Python or pytest) and only this command
    line of it is refused, the gate's own sentence is returned. A program spelled as a share or a
    device path is refused without being looked at: the gate would resolve it, which opens it, and it
    is never the interpreter running VOOL. An empty argv is left to the job's own checks.
    """
    if not _is_windows_platform():
        return None
    items = [str(item) for item in argv]
    if not items or not items[0].strip():
        return None
    program = items[0]
    from core import windows_path_policy

    if windows_path_policy.classify(program, network_only=True):
        return WINDOWS_PROGRAM_REFUSED_TEXT.format(program=program)
    try:
        JobRunner._windows_phase1_argv(items)
        return None
    except KernelIsolationUnavailableError as exc:
        refused = str(exc)
    try:
        JobRunner._windows_phase1_argv([program, "-c", "pass"])
    except KernelIsolationUnavailableError:
        return WINDOWS_PROGRAM_REFUSED_TEXT.format(program=program)
    return refused


#: The programs whose Windows verdict the capability report states (core/runtime_capabilities.py):
#: the ones VOOL's own tools ask for and the README's Windows limits name. Only the names live
#: here; each verdict comes from `JobRunner._windows_phase1_argv`, the gate a real job meets.
WINDOWS_REPORTED_PROGRAMS = ("python", "pytest", "git", "npm", "node", "ruff", "pip")


def windows_sandbox_report() -> dict | None:
    """What the Windows sandbox runs, read from the gates a job meets, or None off Windows.

    - `confined_programs`: the program names phase 1 accepts (`_WINDOWS_PYTHON_NAMES`,
      `_WINDOWS_PYTEST_NAMES`); every other program is refused before anything launches.
    - `programs`: each name in `WINDOWS_REPORTED_PROGRAMS` with the verdict
      `_windows_phase1_argv` gives it as a bare program name ("confined" or "refused").
    - `backend`: "launcher_missing" when no pinned VoolConfine launcher verifies, "proven" or
      "probe_failed" from the probe the first job runs (cached per process), and "not_probed"
      until a job has asked. This report never runs the probe: it starts a confined child, too
      slow for a status read. Only "proven" means a confined job has run here. Any error while
      looking reads as "launcher_missing" (fail closed)."""
    if not _is_windows_platform():
        return None
    verdicts: dict[str, str] = {}
    for name in WINDOWS_REPORTED_PROGRAMS:
        try:
            JobRunner._windows_phase1_argv([name])
        except KernelIsolationUnavailableError:
            verdicts[name] = "refused"
        else:
            verdicts[name] = "confined"
    backend = "launcher_missing"
    try:
        from sandbox import windows_confinement

        launcher = windows_confinement.launcher_path(_windows_project_root())
        if launcher is not None:
            usable = _BACKEND_USABLE.get(_windows_probe_key(launcher))
            backend = "not_probed" if usable is None else "proven" if usable else "probe_failed"
    except Exception:
        backend = "launcher_missing"
    return {
        "confined_programs": sorted(_WINDOWS_PYTHON_NAMES | _WINDOWS_PYTEST_NAMES),
        "programs": verdicts,
        "backend": backend,
    }
