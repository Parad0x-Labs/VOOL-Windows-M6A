"""Windows installed-bundle updater: payload checks, journaled in-folder swap, process helper.

The Windows install is the per-user Inno Setup folder (``{app}``, by default
``%LOCALAPPDATA%\\Programs\\VOOL``). The installer owns a fixed set of top-level entries in it
(:data:`MANAGED_ENTRIES`). An update replaces exactly those entries and nothing else:

* the signed payload ZIP must hold exactly the managed entries, no more and no fewer;
* staging lives in ``{app}\\.vool-update`` and the previous version is kept in
  ``{app}\\update_backups\\windows-<txid>``, both inside the program folder, so every move is a
  rename on one volume and nothing is written beside the install or in the user's data folder;
* every other entry in ``{app}`` (Inno's ``unins000.*``, the backups, the staging folder) is
  never moved, overwritten or deleted;
* each move is journaled first (``installer.self_update`` format), so a failed or interrupted
  swap is put back by :func:`installer.self_update.rollback_swap`.

Integrity is the shared Ed25519 manifest + per-artifact signature + SHA-256 (verified before this
module sees the payload). The bundle's own executables are not Authenticode-signed today, so
``verify_bundle`` checks the payload's exact shape and reports ``codesigned=False`` honestly.

The process helper stops only VOOL's own processes: those whose executable lives under the
program folder's ``python``, ``ollama`` or ``broker`` directories. It first asks the bundle
supervisor to stop (the same ``run\\stop`` request the Stop button sends), then ends any such
process still running. Anything else, including a Python the user put elsewhere, is never touched.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from core.updater.platforms import BundleVerification, SwapOutcome

logger = logging.getLogger("vool.updater.windows")

#: Every top-level entry the Inno Setup script installs into ``{app}`` (installer/bundle/vool.iss).
MANAGED_ENTRIES: tuple[str, ...] = (
    "app",
    "broker",
    "bundle_manifest.json",
    "bundle_supervisor.py",
    "ollama",
    "python",
    "VOOL.cmd",
    "vool-open.ps1",
    "vool.vbs",
    "vool_protocol_handler.py",
    "vool_window.py",
)
_MANAGED_DIRECTORIES = frozenset({"app", "broker", "ollama", "python"})
#: Files that must exist inside a payload for it to be a runnable Windows bundle.
_REQUIRED_FILES: tuple[str, ...] = (
    "app/apps/vool_api_server.py",
    "app/core/app_version.py",
    "app/core/updater/windows.py",
    "broker/VoolBroker.exe",
    "ollama/ollama.exe",
    "python/python.exe",
    "python/pythonw.exe",
)
#: Executables under these program-folder directories are VOOL's own processes.
OWNED_PROCESS_DIRECTORIES: tuple[str, ...] = ("python", "ollama", "broker")
BUNDLE_MANIFEST_SCHEMA = "vool.bundle_manifest.v1"
STAGING_DIR_NAME = ".vool-update"
BACKUPS_DIR_NAME = "update_backups"
PRIOR_PREFIX = "windows-"
UPDATING_LOCK_NAME = ".vool_updating.lock"
SWAP_COMPLETE_NAME = "_complete.json"
MAX_PAYLOAD_ENTRIES = 100_000

# Process-creation flags (documented values; subprocess names only some of them).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _fold(name: str) -> str:
    return name.casefold()


_MANAGED_FOLDED = {_fold(name): name for name in MANAGED_ENTRIES}


def _is_reparse_point(path: Path) -> bool:
    """A symlink, junction or any other reparse point: never followed, never accepted."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def read_bundle_manifest(root: Path) -> dict:
    """The bundle's own ``bundle_manifest.json`` (PowerShell writes it with a UTF-8 BOM)."""
    data = json.loads((Path(root) / "bundle_manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema") != BUNDLE_MANIFEST_SCHEMA:
        raise ValueError("bundle_manifest.json is not a VOOL Windows bundle manifest")
    return data


def looks_like_installed_bundle(root: Path) -> bool:
    """True for a VOOL install folder made by VOOL-Setup.exe (not a source checkout)."""
    root = Path(root)
    try:
        read_bundle_manifest(root)
    except (OSError, ValueError):
        return False
    return (root / "python" / "python.exe").is_file() and (root / "app" / "core").is_dir()


def payload_identity_problem(bundle: Path, *, version: str, build_commit: str = "",
                             require_commit: bool = True) -> str:
    """Empty when the payload is a clean build of exactly this signed offer; else why not.

    A Windows offer must pin the exact 40-hex build commit (the builder always records one);
    an offer without it is refused, never waved through.
    """
    import re

    if require_commit and not re.fullmatch(r"[0-9a-f]{40}", str(build_commit or "")):
        return "the signed offer does not pin the exact Windows build commit"
    try:
        data = read_bundle_manifest(bundle)
    except (OSError, ValueError) as exc:
        return f"the Windows payload has no valid bundle manifest: {exc}"
    if str(data.get("app_version") or "") != str(version):
        return (f"the Windows payload is version {data.get('app_version')!r}, "
                f"but the signed offer is {version!r}")
    if data.get("source_dirty_state") is not False:
        return "the Windows payload was not built from a clean source tree"
    if build_commit and str(data.get("source_commit") or "") != build_commit:
        return "the Windows payload does not match the signed build commit"
    return ""


class WindowsBundleInstaller:
    name = "windows-bundle"

    # -- locations ---------------------------------------------------------- #

    @staticmethod
    def staging_root(target_app: Path) -> Path:
        """Where the flow unpacks a payload: inside the program folder, same volume."""
        return Path(target_app) / STAGING_DIR_NAME

    @staticmethod
    def prior_path(target_app: Path, txid: str) -> Path:
        return Path(target_app) / BACKUPS_DIR_NAME / f"{PRIOR_PREFIX}{txid}"

    # -- verification ------------------------------------------------------- #

    def verify_bundle(self, staged_bundle: Path, *, require_notarization: bool | None = None) -> BundleVerification:
        bundle = Path(staged_bundle)
        if not bundle.is_dir() or _is_reparse_point(bundle):
            return BundleVerification(False, detail=f"no such payload folder: {bundle}")
        entries = {}
        for child in bundle.iterdir():
            folded = _fold(child.name)
            if folded not in _MANAGED_FOLDED or child.name != _MANAGED_FOLDED[folded]:
                return BundleVerification(False, detail=f"payload holds an unexpected entry: {child.name}")
            entries[child.name] = child
        missing = sorted(set(MANAGED_ENTRIES) - set(entries))
        if missing:
            return BundleVerification(False, detail=f"payload is missing: {', '.join(missing)}")
        for name, child in entries.items():
            want_dir = name in _MANAGED_DIRECTORIES
            if _is_reparse_point(child) or (child.is_dir() if not want_dir else not child.is_dir()):
                return BundleVerification(False, detail=f"payload entry has the wrong type: {name}")
        count = 0
        for directory, dirs, files in os.walk(bundle, followlinks=False):
            for item in [*dirs, *files]:
                count += 1
                path = Path(directory) / item
                if count > MAX_PAYLOAD_ENTRIES:
                    return BundleVerification(False, detail="payload has too many entries")
                if _is_reparse_point(path):
                    return BundleVerification(False, detail=f"payload holds a link: {path.relative_to(bundle)}")
                mode = os.lstat(path).st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    return BundleVerification(False, detail=f"payload holds a special file: {path.relative_to(bundle)}")
        for relative in _REQUIRED_FILES:
            if not (bundle / relative).is_file():
                return BundleVerification(False, detail=f"payload is missing {relative}")
        try:
            read_bundle_manifest(bundle)
        except (OSError, ValueError) as exc:
            return BundleVerification(False, detail=f"payload bundle manifest is invalid: {exc}")
        return BundleVerification(
            True, codesigned=False, notarized=False,
            detail="publisher-verified payload (Ed25519 + SHA-256); executables are not Authenticode-checked",
        )

    # -- swap --------------------------------------------------------------- #

    def atomic_swap(self, staged_bundle: Path, target_app: Path, *, txid: str) -> SwapOutcome:
        staged = Path(staged_bundle)
        target = Path(target_app)
        prior = self.prior_path(target, str(txid))
        if not looks_like_installed_bundle(target):
            return SwapOutcome(False, detail=f"not an installed VOOL bundle: {target}")
        if not staged.is_dir():
            return SwapOutcome(False, detail=f"staged payload missing: {staged}")
        try:
            if staged.stat().st_dev != target.stat().st_dev:
                return SwapOutcome(False, detail="staged payload is on a different volume than the app")
        except OSError as exc:
            return SwapOutcome(False, detail=f"cannot stat swap endpoints: {exc}")
        if prior.exists():
            return SwapOutcome(False, detail=f"a backup already exists for this transaction: {prior}")
        try:
            prior.mkdir(parents=True, exist_ok=False)
            _swap_entries(staged, target, prior)
            _write_journal(prior / SWAP_COMPLETE_NAME, {"txid": str(txid), "entries": list(MANAGED_ENTRIES)})
            # Every staged entry was renamed into place: only empty folders remain.
            with contextlib.suppress(OSError):
                staged.rmdir()
                if staged.parent.name.startswith(".vool-stage-"):
                    staged.parent.rmdir()
            return SwapOutcome(True, prior_path=prior)
        except Exception as exc:
            if (prior / "_replaced.json").is_file():
                try:
                    restore_entries(target, prior)
                except Exception as restore:
                    return SwapOutcome(False, prior_path=prior,
                                       detail=f"app swap failed: {exc}; restore failed: {restore}")
                with contextlib.suppress(OSError):
                    shutil.rmtree(prior)
            elif prior.is_dir():
                with contextlib.suppress(OSError):
                    prior.rmdir()
            return SwapOutcome(False, prior_path=prior if prior.exists() else None,
                               detail=f"app swap refused: {exc}")

    @staticmethod
    def swap_complete(prior_path: Path) -> bool:
        """True only when every managed entry was swapped (the journal's last write).

        A crash between two renames leaves old and new entries mixed; such an install can
        still answer its health check, so recovery must restore it instead of finalizing it.
        """
        return (Path(prior_path) / SWAP_COMPLETE_NAME).is_file()

    def restore_prior(self, target_app: Path, prior_path: Path, *, txid: str) -> SwapOutcome:
        prior = Path(prior_path)
        try:
            if prior.is_dir() and not _is_reparse_point(prior) and not any(prior.iterdir()):
                # Reserved before the first journal write: nothing was moved yet.
                return SwapOutcome(True, prior_path=prior, detail="the swap had not started")
            if not (prior / "_replaced.json").is_file():
                raise ValueError("backup is not a Windows update swap journal")
            restore_entries(Path(target_app), prior)
            return SwapOutcome(True, prior_path=prior)
        except Exception as exc:
            return SwapOutcome(False, prior_path=prior, detail=f"could not restore the previous app: {exc}")

    def prune_priors(self, target_app: Path, *, keep: int = 2) -> list[Path]:
        root = Path(target_app) / BACKUPS_DIR_NAME
        candidates = [p for p in root.glob(f"{PRIOR_PREFIX}*") if p.is_dir() and not _is_reparse_point(p)]
        # A backup whose entries were already moved back (a rollback ran) holds only its
        # journal: it is not a previous version and never counts toward the retention.
        complete = [p for p in candidates if all((p / name).exists() for name in MANAGED_ENTRIES)]
        spent = [p for p in candidates if not any((p / name).exists() for name in MANAGED_ENTRIES)
                 and (p / "_replaced.json").is_file()]
        complete.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        removed = []
        for stale in [*complete[max(1, int(keep)):], *spent]:
            shutil.rmtree(stale)
            removed.append(stale)
        return removed


def _write_journal(path: Path, payload: object) -> None:
    temporary = path.with_suffix(".tmp")
    # Windows only flushes a handle opened for writing, so sync while it is still open.
    with temporary.open("w", encoding="utf-8") as durable:
        durable.write(json.dumps(payload, sort_keys=True))
        durable.flush()
        os.fsync(durable.fileno())
    os.replace(temporary, path)


RENAME_ATTEMPTS = 6
RENAME_RETRY_SECONDS = 0.4
DISCARDED_DIR_NAME = "_discarded"


def _rename(source: Path, destination: Path, *, sleeper: Callable[[float], None] = time.sleep) -> None:
    """os.rename, retried briefly: a scanner (Defender) or Explorer can hold a fresh file for a moment.

    A rename either moves the whole entry or nothing, so retrying is always safe.
    """
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            sleeper(RENAME_RETRY_SECONDS)


def restore_entries(target: Path, prior: Path) -> None:
    """Put the journaled previous entries back using renames only; never delete in place.

    Each swapped-in entry is first renamed aside into ``<prior>/_discarded``, then the original
    is renamed back. A refused rename leaves that entry whole (old or new) and raises, so the
    journal can be replayed; deleting a folder in place could stop half-way on a locked file and
    leave a mix of both versions. The discarded copies are removed last, best effort.
    Idempotent: entries already restored are skipped.
    """
    target, prior = Path(target), Path(prior)
    replaced = json.loads((prior / "_replaced.json").read_text(encoding="utf-8"))
    originals_path = prior / "_originals.json"
    originals = json.loads(originals_path.read_text(encoding="utf-8")) if originals_path.is_file() else {}
    if not isinstance(replaced, list):
        raise ValueError("invalid Windows swap journal")
    discarded = prior / DISCARDED_DIR_NAME
    for name in reversed(replaced):
        if name not in MANAGED_ENTRIES:
            raise ValueError(f"invalid Windows swap journal entry: {name!r}")
        current, original = target / name, prior / name
        had_original = originals.get(name) is True
        if had_original and not original.exists():
            # Never moved aside (or already restored): the installed entry is the old one.
            if not current.exists():
                raise FileNotFoundError(f"previous version of {name} is missing")
            continue
        if current.exists():
            discarded.mkdir(exist_ok=True)
            aside = discarded / name
            if aside.exists():
                raise FileExistsError(f"a discarded copy of {name} is already in the backup")
            _rename(current, aside)
        if original.exists():
            _rename(original, current)
    if discarded.exists():
        shutil.rmtree(discarded, ignore_errors=True)


def _swap_entries(staged: Path, target: Path, prior: Path) -> list[str]:
    """Rename each managed entry aside, then the staged one into place; journal before each move.

    Same journal files as installer.self_update.backup_and_swap; restore_entries undoes a partial
    swap with renames only. A rename Windows keeps refusing (a file still open in that entry)
    stops the swap.
    """
    replaced: list[str] = []
    originals: dict[str, bool] = {}
    for name in MANAGED_ENTRIES:
        current = target / name
        incoming = staged / name
        if not incoming.exists():
            raise FileNotFoundError(f"staged payload has no {name}")
        if current.exists() and _is_reparse_point(current):
            raise ValueError(f"installed entry is a link; refusing to move it: {name}")
        originals[name] = current.exists()
        replaced.append(name)
        _write_journal(prior / "_originals.json", originals)
        _write_journal(prior / "_replaced.json", replaced)
        if current.exists():
            _rename(current, prior / name)
        _rename(incoming, current)
    return replaced


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #

def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def owned_process_roots(app_path: Path) -> tuple[str, ...]:
    return tuple(_norm(Path(app_path) / name) + os.sep for name in OWNED_PROCESS_DIRECTORIES)


def is_owned_executable(executable: str, app_path: Path) -> bool:
    """True when ``executable`` lives under the program folder's python/ollama/broker tree."""
    if not executable:
        return False
    candidate = _norm(executable)
    return any(candidate.startswith(root) for root in owned_process_roots(app_path))


def _default_process_table() -> Iterable[tuple[int, str, object]]:
    import psutil

    for process in psutil.process_iter(["pid", "exe"]):
        with contextlib.suppress(Exception):
            yield int(process.info["pid"]), str(process.info.get("exe") or ""), process


def _default_kill(process: object) -> None:
    """Kill through the enumerated psutil.Process: psutil checks its creation time first, so a
    pid the OS reused for someone else's process raises NoSuchProcess instead of killing it."""
    import psutil

    with contextlib.suppress(psutil.NoSuchProcess):
        process.kill()


def _system_wscript() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return str(Path(root) / "System32" / "wscript.exe")


_SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONDONTWRITEBYTECODE",
                 "VOOL_PROJECT_ROOT", "VOOL_EXPECTED_COMMIT", "VOOL_RUNTIME_MODE",
                 "VOOL_OWNED_BY_WINDOW_PID", "VOOL_NATIVE_PARENT_PID")


class WindowsProcessHelper:
    """Stop VOOL's own processes before the swap, start the installed launcher after it."""

    def __init__(
        self,
        *,
        app_path: Path,
        vool_home: Path | None,
        pid_file: Path | None = None,
        process_table: Callable[[], Iterable[tuple]] | None = None,
        kill: Callable[[object], None] | None = None,
        popen: Callable[..., object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        grace_seconds: float = 20.0,
        poll_interval: float = 0.25,
    ):
        self.app_path = Path(app_path)
        self.vool_home = Path(vool_home) if vool_home else None
        self.pid_file = Path(pid_file) if pid_file else None
        self.lock_path = self.app_path / UPDATING_LOCK_NAME
        self._table = process_table or _default_process_table
        self._kill = kill or _default_kill
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self._sleep = sleeper
        self.grace_seconds = float(grace_seconds)
        self.poll_interval = float(poll_interval)
        self.killed: list[int] = []

    def _owned(self) -> dict[int, object]:
        me = os.getpid()
        owned: dict[int, object] = {}
        for row in self._table():
            pid, exe = int(row[0]), str(row[1] or "")
            if pid != me and pid > 0 and is_owned_executable(exe, self.app_path):
                owned[pid] = row[2] if len(row) > 2 else pid
        return owned

    def owned_pids(self) -> list[int]:
        return sorted(self._owned())

    def shutdown_app(self) -> None:
        self.lock_path.write_text("signed update in progress\n", encoding="utf-8")
        if self.vool_home is not None:
            from installer.vool_stop import signal_bundle_supervisor

            signal_bundle_supervisor(self.vool_home)

    def wait_until_stopped(self, *, timeout: float) -> bool:
        deadline = self._clock() + max(0.0, float(timeout))
        force_at = self._clock() + min(self.grace_seconds, max(0.0, float(timeout)) * 2 / 3)
        while True:
            remaining = self._owned()
            if not remaining:
                return True
            now = self._clock()
            if now >= deadline:
                return False
            if now >= force_at:
                for pid, handle in sorted(remaining.items()):
                    try:
                        self._kill(handle)
                        self.killed.append(pid)
                    except Exception as exc:  # access denied etc.: report by staying "running"
                        logger.warning("could not end VOOL process %s: %s", pid, exc)
                force_at = deadline  # one forced pass; then wait for the kernel to reap them
            self._sleep(self.poll_interval)

    def stop_running_app(self, *, timeout: float = 30.0) -> bool:
        self.shutdown_app()
        return self.wait_until_stopped(timeout=timeout)

    def relaunch(self, app_path: Path) -> bool:
        root = Path(app_path)
        launcher = root / "vool.vbs"
        if not launcher.is_file():
            logger.error("installed launcher missing: %s", launcher)
            return False
        if self.vool_home is not None:
            with contextlib.suppress(OSError):
                (self.vool_home / "run" / "stop").unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        environment = dict(os.environ)
        for name in _SCRUBBED_ENV:
            environment.pop(name, None)
        try:
            self._popen([_system_wscript(), str(launcher)], cwd=str(root), env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        close_fds=True, creationflags=DETACHED_FLAGS)
            return True
        except OSError as exc:
            logger.error("relaunch failed: %s", exc)
            return False


# --------------------------------------------------------------------------- #
# Worker start by the bundle supervisor
# --------------------------------------------------------------------------- #

WORKER_REQUEST_NAME = "update-worker.json"
_TXID_PATTERN = r"[A-Za-z0-9._-]{1,128}"


def consume_worker_request(home: Path, app_root: Path, *, popen: Callable[..., object] | None = None,
                           copytree: Callable[..., object] = shutil.copytree,
                           now: Callable[[], float] = time.time) -> int | None:
    """Start the updater worker the API asked for; called by the bundle supervisor's loop.

    The supervisor is outside its own kill-on-close job, so a worker it starts (not assigned
    to the job, not a descendant of the API) survives the stop the swap needs. Because that
    makes it a way out of the job, nothing it runs comes from the user-writable VOOL home:

    * the request names only a transaction id; the job folder and plan are derived from it;
    * the plan must name this transaction, a Windows platform and THIS program folder;
    * the frozen interpreter and code are copied here, now, from the program folder the
      supervisor runs from, into ``python`` and ``app`` (the layout the interpreter's ._pth
      expects). A job folder that already holds either is refused (something other than the
      updater put it there), so a planted pythonw.exe or code tree never runs;
    * the worker's trust keys come from that frozen code, not from the plan (worker.main).

    Anything else is refused and the request is discarded. Returns the worker's pid, or None
    when nothing was started.
    """
    import re

    from core.updater.state import _atomic_write_json, _read_json
    from core.updater.worker import FROZEN_CODE_TREES, WINDOWS_WORKER_APP_ENV

    home, app_root = Path(home), Path(app_root).resolve()
    request = home / "run" / WORKER_REQUEST_NAME
    if not request.is_file():
        return None
    taken = request.with_name(request.name + ".taken")
    try:
        os.replace(request, taken)
        payload = _read_json(taken)
    finally:
        with contextlib.suppress(OSError):
            taken.unlink()

    def refuse(reason: str) -> None:
        logger.warning("update worker request refused: %s", reason)

    txid = str((payload or {}).get("txid") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(_TXID_PATTERN, txid) or txid in (".", ".."):
        return refuse("invalid transaction id")
    if not looks_like_installed_bundle(app_root):
        return refuse(f"{app_root} is not an installed VOOL bundle")
    updater_root = (home / "data" / "update_v2").resolve()
    job = updater_root / "workers" / txid
    # The bundle's embeddable Python ignores PYTHONPATH and the working folder: its ._pth names
    # "..\app", so the frozen code must sit at the interpreter copy's sibling "app", exactly as
    # in the program folder. "code" is refused too (the older layout's name).
    plan, code, runtime = job / "plan.json", job / "app", job / "python"
    if not job.is_dir() or _is_reparse_point(job) or not plan.is_file() or _is_reparse_point(plan):
        return refuse(f"{job} holds no prepared plan")
    if any(os.path.lexists(job / name) for name in ("app", "code", "python")):
        return refuse(f"{job} already holds code or an interpreter the supervisor did not copy")
    plan_data = _read_json(plan)
    feed = plan_data.get("feed") if isinstance(plan_data, dict) else None
    if (not isinstance(feed, dict) or plan_data.get("txid") != txid
            or not str(plan_data.get("platform") or "").startswith("windows-")
            or _norm(str(feed.get("app_path") or "")) != _norm(app_root)):
        return refuse("the plan does not name this transaction and this program folder")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    copytree(app_root / "python", runtime, symlinks=True, ignore=ignore)
    for name in FROZEN_CODE_TREES:
        copytree(app_root / "app" / name, code / name, symlinks=True, ignore=ignore)
    # The interpreter's import path comes from its ._pth: it must be the program folder's, byte for byte.
    shipped = sorted(path.name for path in (app_root / "python").glob("*._pth"))
    frozen = sorted(path.name for path in runtime.glob("*._pth"))
    if shipped != frozen or any((runtime / name).read_bytes() != (app_root / "python" / name).read_bytes()
                                for name in shipped):
        return refuse("the frozen interpreter's ._pth differs from the program folder's")
    environment = {key: value for key, value in os.environ.items()
                   if key not in _SCRUBBED_ENV and key != "VOOL_UPDATE_TRUSTED_KEYS_JSON"}
    environment.update(PYTHONPATH=str(code), PYTHONDONTWRITEBYTECODE="1", VOOL_HOME=str(home))
    environment[WINDOWS_WORKER_APP_ENV] = str(app_root)
    with (job / "worker.log").open("ab") as output:
        process = (popen or subprocess.Popen)(
            [str(runtime / "pythonw.exe"), "-m", "core.updater.worker", str(plan)], cwd=str(code),
            env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True,
            creationflags=DETACHED_FLAGS,
        )
    lease = {"pid": int(process.pid), "plan": str(plan), "txid": txid, "started_at": now(),
             "scope": "", "launcher": "supervisor"}
    _atomic_write_json(job / "process.json", lease)
    _atomic_write_json(updater_root / "worker.json", lease)
    return int(process.pid)


__all__ = [
    "MANAGED_ENTRIES",
    "WindowsBundleInstaller",
    "WindowsProcessHelper",
    "consume_worker_request",
    "is_owned_executable",
    "looks_like_installed_bundle",
    "payload_identity_problem",
]
