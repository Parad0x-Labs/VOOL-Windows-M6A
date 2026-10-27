"""Windows bundle supervisor for the Ollama and VOOL API child processes."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from core.runtime_paths import native_runtime_home

# Every age, retry time and unanswered span below is measured on _tick(), never the wall clock: a
# clock set forward or back is not child age. _tick() is time.monotonic moved on by a fixed origin, so
# a 0 left in started_at or next_attempt always reads as long past, however recently the host booted.
# On Windows monotonic is GetTickCount64, which DOES count time the machine sleeps; _absorb_pause
# takes such a pause back out.
_TICK_ORIGIN_SECONDS = 86_400.0
_POLL_SECONDS = 2.0
# A child that has never answered its health check is stopped once it is this old.
_STARTUP_TIMEOUT_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 30.0
# Each health probe waits at most this long per socket step, as the window host's probe does.
_PROBE_TIMEOUT_SECONDS = 1.5
# A child that HAS answered is stopped only after this many health checks in a row went unanswered
# AND those checks span at least its time here. One slow answer (a busy API, a model pull on a slow
# disk) is never a restart. The API's span is longer than the window host's own 30 s grace
# (native_runtime_supervisor unhealthy_grace), so the window decides a stall of the API: one of up
# to 30 s keeps the API and the window; a longer one closes the window on its own grace (exit 3),
# and it reopens on the same API once that answers again. An API that stays hung is restarted 40 s
# plus up to two passes after its first unanswered check (about 45 s; 48 s with Ollama hung too; up
# to about 56 s when an Ollama stop with an 8 s taskkill falls inside the API's 40 s), and the window
# reopens on the new one. Ollama has no window watching it, and a restart drops its loaded model and
# any pull in progress.
_UNANSWERED_CHECKS_BEFORE_RESTART = 3
_UNANSWERED_SECONDS_BEFORE_RESTART = {"api": 40.0, "ollama": 60.0}
# The restart backoff forgets earlier failures once a child has kept answering this long; a child
# that answers once and then hangs keeps backing off further.
_ANSWERING_SECONDS_BEFORE_BACKOFF_RESET = 60.0
# A wait that overruns what it should take by more than this means this process was not running
# (sleep, hibernation, a paused VM): the overrun is not counted as child age or unanswered time.
# The supervisor's own work (a stop, a start, the state write, the stale-API scan) is awake time up
# to its own limit; an overrun of more than this beyond that limit is a pause too. So a sleep that
# starts inside a stop or the scan, or just before the scan, is not seen while the step and the
# sleep together stay within 23 s or 35 s (18 s or 30 s plus this), and it then counts in full as
# awake time toward every child's age and run of unanswered checks. Each span is measured once,
# from where the one before it ended (_absorb_pause).
_PAUSE_SECONDS = 5.0
# The longest a stop of a child can take awake: taskkill's 8 s, then 5 s for the child to exit and
# 5 s more after a kill.
_STOP_SECONDS = 18.0
# The PowerShell scan for stale VOOL API processes is given up after this long, so a hung WMI never
# stops the supervisor from watching Ollama and the window.
_STALE_API_CLEANUP_TIMEOUT_SECONDS = 30.0
# A relaunch that arrives while the window is still closing (the user clicked X, then the
# shortcut) still reopens it if that window exits within this many seconds.
_REOPEN_GRACE_SECONDS = 10.0
# A window reopened after its runtime was lost whose host exits 1 within this much awake time of its
# start never attached: the host checks the API once as it starts, and exits 1 at once without
# showing a window when that check misses. Its life is measured up to the first measured instant
# after its exit (_absorb_pause notes the exit there), not up to the pass that finds it exited: that
# overshoots the exit by at most one step of the supervisor's own work, the step's own time plus
# _PAUSE_SECONDS (35 s for the stale-API scan). Such a window is opened again with the restart
# backoff, at most _WINDOW_REOPEN_RETRIES times in a row (2 + 4 + 8 + 16 + 30 + 30 s, about 90 s of
# tries), and each new runtime loss starts the tries over. A reopened window that lived longer had
# attached: its exit 1 is a forced end, and it stays closed.
_WINDOW_ATTACH_SECONDS = 60.0
_WINDOW_REOPEN_RETRIES = 6
_WINDOWS_SCRIPT_ARGUMENT_PREFIX = (
    r'^(?:\x22(?:[^\x22]*\\)?pythonw?\.exe\x22|(?:[^\s\x22]*\\)?pythonw?\.exe)'
    r'(?:\s+-[bBdEhIiOPqRsSuvVx]+'
    r'|\s+-W(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)'
    r'|\s+-X(?:\s+\x22[^\x22]*\x22|\s+\S+|[^\s\x22]+)'
    r'|\s+--check-hash-based-pycs\s+(?:\x22[^\x22]*\x22|\S+))*\s+\x22?'
)
# PowerShell ends a single-quoted string at the curly quotes U+2018, U+2019, U+201A and U+201B as
# well as at the ASCII apostrophe, and reads any of them doubled as that one character.
_POWERSHELL_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


_NOTES_COPY_MARKER = "notes-copied-from-program-folder.json"


class _NoProbeRedirect(urllib.request.HTTPRedirectHandler):
    """A health check judges the answer of 127.0.0.1 itself, never a page it points elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# No proxy: urllib otherwise sends a 127.0.0.1 probe to the proxy in HTTP_PROXY or the Windows
# Internet settings (their "<local>" bypass covers only names without a dot). The window host's
# probe already goes direct; the two must agree on whether the API answers.
_PROBE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoProbeRedirect())


def _tick() -> float:
    """The clock of every child age, retry time and unanswered span (see the top of this file)."""
    return time.monotonic() + _TICK_ORIGIN_SECONDS


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)  # Python 3.12+
    return path.is_symlink() or bool(is_junction and is_junction())


def _copy_notes_tree(source: Path, target: Path) -> dict[str, Any]:
    """Copy the regular files under `source` into `target`, keeping relative paths.

    Links and junctions (the folder itself, a subfolder or a file) are skipped, so nothing outside
    the old notes folder is copied. A file `target` already has is kept as it is. Raises OSError on
    a failed copy, or when `target` (or a folder on the way into it) is a link.
    """
    report: dict[str, Any] = {"copied": [], "kept": [], "skipped": []}
    if _is_link(source):
        report["skipped"].append(".")
        return report
    if not source.is_dir():
        return report
    for current, dirs, files in os.walk(source, followlinks=False):
        here = Path(current)
        relative_dir = here.relative_to(source)
        for name in list(dirs):
            if _is_link(here / name):
                dirs.remove(name)
                report["skipped"].append(str(relative_dir / name))
        destination_dir = target / relative_dir
        for part in [target, *[target / Path(*relative_dir.parts[: i + 1]) for i in range(len(relative_dir.parts))]]:
            if _is_link(part):
                raise OSError(f"{part} is a link, not a folder in the VOOL home")
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            item, relative = here / name, str(relative_dir / name)
            if _is_link(item) or not item.is_file():
                report["skipped"].append(relative)
                continue
            destination = destination_dir / name
            if destination.exists() or destination.is_symlink():
                report["kept"].append(relative)
                continue
            # Copy to a temporary name, then link it into place: a crash mid-copy can leave only the
            # temporary file, never a truncated note that the retry would keep as already present.
            temporary = destination_dir / f".{name}.{uuid.uuid4().hex}.vool-copy.tmp"
            try:
                with temporary.open("xb") as writer, item.open("rb") as reader:
                    writer.write(reader.read())
                with contextlib.suppress(OSError):
                    os.utime(temporary, ns=(item.stat().st_atime_ns, item.stat().st_mtime_ns))
                try:
                    os.link(temporary, destination)  # never replaces an existing file
                except FileExistsError:
                    report["kept"].append(relative)
                    continue
                except OSError:
                    # No hard links on this volume (FAT, exFAT, some network shares): rename instead.
                    # On Windows a rename never replaces a file; elsewhere the check just before it
                    # keeps an existing note.
                    if destination.exists() or destination.is_symlink():
                        report["kept"].append(relative)
                        continue
                    try:
                        os.rename(temporary, destination)
                    except FileExistsError:
                        report["kept"].append(relative)
                        continue
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            report["copied"].append(relative)
    return report


def _is_windows_platform() -> bool:
    """Indirection over ``os.name == "nt"`` so tests can simulate "running on Windows" for one
    call without mutating the real ``os.name`` -- ``pathlib.Path`` itself branches on ``os.name``
    to choose ``WindowsPath``/``PosixPath``, so patching the global attribute directly breaks
    every OTHER ``Path(...)`` construction for the duration of the patch, including ones made
    deep inside pytest's own failure reporting (measured: full run 36063857499 shard 5 died with
    ``cannot instantiate 'WindowsPath'`` inside ``_pytest``'s traceback formatter while rendering
    an assertion failure, because a test had flipped the global)."""
    return os.name == "nt"


class BundleSupervisor:
    def __init__(self, root: Path, *, env: dict[str, str] | None = None) -> None:
        self.root = root.resolve()
        self.env = dict(os.environ if env is None else env)
        self.home = native_runtime_home(self.env)
        self.run_dir = self.home / "run"
        self.log_dir = self.home / "logs"
        self.state_path = self.run_dir / "bundle-supervisor.json"
        self.lock_path = self.run_dir / "bundle-supervisor.lock"
        self.stop_path = self.run_dir / "stop"
        self.window_reopen_path = self.run_dir / "window-reopen"
        self.model_status_path = self.run_dir / "model-pull.json"
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.handles: dict[str, Any] = {}
        self.started_at: dict[str, float] = {}
        self.failures: dict[str, int] = {"ollama": 0, "api": 0, "window": 0}
        self.restart_counts: dict[str, int] = {"ollama": 0, "api": 0, "window": 0}
        self.next_attempt: dict[str, float] = {"ollama": 0.0, "api": 0.0, "window": 0.0}
        self.last_exit: dict[str, dict[str, Any]] = {}
        self.health: dict[str, dict[str, Any]] = {}
        # When the current child first answered, and the run of unanswered checks since then.
        self.answered_at: dict[str, float] = {}
        self.unanswered: dict[str, int] = {}
        self.unanswered_since: dict[str, float] = {}
        # Where the last measured span ended (_absorb_pause); None before the first.
        self._last_activity: float | None = None
        self.supervisor_started_at = time.time()
        self._lock_handle: Any | None = None
        self.window_started = False
        self.window_reopen_until = 0.0
        # Set by a window's exit 3 and cleared by any exit but 1: the window opening or open now is
        # one this supervisor reopened after its runtime was lost.
        self._window_lost_runtime = False
        # The first measured instant at which such a window was seen exited (_absorb_pause).
        self.exit_seen_at: dict[str, float] = {}

    def _manifest(self) -> dict[str, Any]:
        path = self.root / "bundle_manifest.json"
        try:
            # Windows PowerShell 5 emits UTF-8 JSON with a BOM by default.
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _bundle_commit(self) -> str:
        """The exact source commit build_bundle.ps1 stamped into this bundle, or empty."""
        path = self.root / "app" / "config" / "build-source.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return ""
        return str(payload.get("commit_full") or "").strip() if isinstance(payload, dict) else ""

    def _model(self) -> str:
        payload = self._manifest()
        return str(payload.get("selected_model") or payload.get("model") or "").strip()

    def _model_store(self) -> Path:
        payload = self._manifest()
        raw = str(payload.get("model_store") or "").strip().replace("\\", "/")
        while "//" in raw:
            raw = raw.replace("//", "/")
        runtime = self.home
        if raw:
            local_prefix = "%LOCALAPPDATA%/VOOL"
            home_prefix = "%VOOL_HOME%"
            lowered = raw.lower()
            if lowered == local_prefix.lower() or lowered.startswith(local_prefix.lower() + "/"):
                suffix = raw[len(local_prefix) :].lstrip("/")
                return runtime / suffix if suffix else runtime
            if lowered == home_prefix.lower() or lowered.startswith(home_prefix.lower() + "/"):
                suffix = raw[len(home_prefix) :].lstrip("/")
                return runtime / suffix if suffix else runtime
        return runtime / "models"

    def _prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.env["VOOL_BUNDLE_ROOT"] = str(self.root)
        self.env["VOOL_BUNDLE_MANIFEST"] = str(self.root / "bundle_manifest.json")
        self.env["VOOL_HOME"] = str(self.home)
        model_store = self._model_store()
        self.env["OLLAMA_MODELS"] = str(model_store)
        model = self._model()
        if model:
            self.env["VOOL_BUNDLE_MODEL"] = model
            self.env["VOOL_OLLAMA_MODEL"] = model
        model_store.mkdir(parents=True, exist_ok=True)
        self._write_model_status("delegated_to_api", "The API owns the non-blocking model pull.")
        self._copy_program_folder_notes_once()

    def _notes_workspace_root(self) -> Path:
        """The workspace the API this supervisor starts keeps its notes under (core.operator.notes.
        _workspace_root with VOOL_BUNDLE_ROOT set): VOOL_WORKSPACE_ROOT when the user set it, else
        the workspace folder under the VOOL home."""
        override = self._workspace_override()
        return Path(override).expanduser() if override else self.home / "workspace"

    def _workspace_override(self) -> str:
        """VOOL_WORKSPACE_ROOT, else the legacy NULLA_WORKSPACE_ROOT, as core.runtime_paths.vool_env reads them."""
        for name in ("VOOL_WORKSPACE_ROOT", "NULLA_WORKSPACE_ROOT"):
            value = str(self.env.get(name) or "").strip()
            if value:
                return value
        return ""

    def _copy_program_folder_notes_once(self) -> None:
        """Copy notes an earlier build kept in {app}\\app\\notes to the workspace in the VOOL home.

        Before notes moved to the VOOL home, the installed API kept them in {app}\\app\\notes, which
        uninstall deletes. This runs once (a marker in run\\ records it) before the first API start.
        It copies regular files only, never follows a link or junction, never replaces a file the
        new location already has, and never deletes or changes the old copy. A failed copy leaves
        no marker, so the next start tries again.
        """
        marker = self.run_dir / _NOTES_COPY_MARKER
        if marker.exists():
            return
        if self._workspace_override():
            # The API keeps notes in the user's own chosen folder, where an earlier build kept them
            # too; old program-folder notes are not copied into that project. No marker, so the copy
            # still runs if the override is removed.
            return
        source = self.root / "app" / "notes"
        target = self._notes_workspace_root() / "notes"
        try:
            report = _copy_notes_tree(source, target)
        except OSError as exc:
            self._log(f"notes copy from the program folder failed, will retry next start: {exc}")
            return
        report.update({"source": str(source), "target": str(target), "copied_at_epoch": time.time()})
        with contextlib.suppress(OSError):
            marker.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if report["copied"] or report["kept"] or report["skipped"]:
            self._log(
                f"notes from the program folder: {len(report['copied'])} copied, "
                f"{len(report['kept'])} already present, {len(report['skipped'])} skipped (links)"
            )

    def _write_model_status(self, status: str, detail: str = "") -> None:
        payload = {
            "schema": "vool.bundle_model_pull.v1",
            "model": self._model(),
            "status": str(status),
            "detail": str(detail)[:240],
            "updated_at_epoch": time.time(),
        }
        with contextlib.suppress(OSError):
            self.model_status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n"
        try:
            with (self.log_dir / "bundle-supervisor.log").open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def _acquire_lease(self) -> bool:
        """Allow one supervisor per bundle home, including after an abrupt exit."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if _is_windows_platform():
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii", errors="replace"))
            handle.flush()
        except (ImportError, OSError):
            handle.close()
            return False
        self._lock_handle = handle
        return True

    def _release_lease(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        with contextlib.suppress(ImportError, OSError):
            handle.seek(0)
            if _is_windows_platform():
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()

    def _healthy(self, url: str) -> bool:
        try:
            with _PROBE_OPENER.open(url, timeout=_PROBE_TIMEOUT_SECONDS) as response:
                # Only a 2xx answer is healthy. The opener raises HTTPError for any other status, a
                # redirect included (_NoProbeRedirect follows none), so a redirect or any other
                # status is unhealthy.
                return 200 <= int(response.status) < 300
        except (OSError, urllib.error.URLError, ValueError, http.client.HTTPException):
            # HTTPException: an answer that is not HTTP (BadStatusLine from another program on the
            # port) is an unhealthy child, not an error that ends run() and stops every child.
            return False

    def _absorb_pause(self, expected: float, where: str) -> None:
        """Measure the span from the last measured instant to now, and take a pause of this whole
        process in it back out of every child's age and unanswered span. The next span starts here,
        so no span is measured twice or skipped; ages and spans are stamped at a measured instant
        (_last_measured), so a pause after one is measured from there.

        A span that overran ``expected`` by more than _PAUSE_SECONDS means the machine slept,
        hibernated or its VM was paused. Only the overrun is taken out, so each pass still counts its
        own poll interval and a hung child is still restarted after a bounded number of passes. The
        answering time that resets the backoff is shifted too: a sleep is not a minute of answers.

        The exit of a window reopened after a runtime loss is noted at the first measured instant
        after it, once this span's pause is taken out: _reap_window measures the window's life to
        there. No other window is polled here."""
        now = _tick()
        since, self._last_activity = self._last_activity, now
        if since is None:
            return
        overrun = now - since - expected
        if overrun > _PAUSE_SECONDS:
            for table in (self.started_at, self.unanswered_since, self.answered_at, self.exit_seen_at):
                for name in table:
                    table[name] += overrun
            self._log(
                f"supervisor was paused {overrun:.0f}s {where} (sleep, hibernation or a paused VM); "
                "not counted as child age or unanswered time"
            )
        window = self.processes.get("window")
        if self._window_lost_runtime and window is not None and "window" not in self.exit_seen_at:
            if window.poll() is not None:
                self.exit_seen_at["window"] = now

    def _last_measured(self) -> float:
        """The instant the last measured span ended (now, before any was measured)."""
        return _tick() if self._last_activity is None else self._last_activity

    def _checked(self, url: str, what: str) -> bool:
        """One health check, with a pause of this process before or during it taken back out. Every
        health check goes through here, the window's included: it runs on every pass until the API
        first answers, and a sleep that starts while it waits is as much a pause as any other."""
        self._absorb_pause(_POLL_SECONDS, f"before {what}")
        healthy = self._healthy(url)
        self._absorb_pause(_PROBE_TIMEOUT_SECONDS, f"during {what}")
        return healthy

    def _health_probe(self, name: str, url: str) -> bool:
        healthy = self._checked(url, f"the {name} health check")
        previous = self.health.get(name, {}).get("healthy")
        self.health[name] = {
            "url": url,
            "healthy": healthy,
            "checked_at": time.time(),
        }
        if previous is None or previous != healthy:
            self._log(f"health {name}={'healthy' if healthy else 'unhealthy'} url={url}")
        return healthy

    def _child_env(self) -> dict[str, str]:
        env = dict(self.env)
        app = str(self.root / "app")
        env["PYTHONPATH"] = app + os.pathsep + str(env.get("PYTHONPATH") or "")
        return env

    def _start(self, name: str, argv: list[str], *, cwd: Path, extra_env: dict[str, str] | None = None) -> None:
        now = _tick()
        if now < self.next_attempt[name]:
            return
        log_path = self.log_dir / f"{name}.log"
        handle = log_path.open("ab")
        creationflags = 0
        if _is_windows_platform():
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        try:
            child = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env={**self._child_env(), **(extra_env or {})},
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception as exc:
            handle.close()
            self._schedule_retry(name, f"failed to start: {type(exc).__name__}: {exc}")
            return
        # Our own work, expected to be quick: awake time up to that, a pause beyond (see _PAUSE_SECONDS).
        # The child's age starts once it exists.
        self._absorb_pause(0.0, f"while starting {name}")
        self.processes[name] = child
        self.handles[name] = handle
        self.started_at[name] = self._last_measured()
        self.exit_seen_at.pop(name, None)
        self._forget_answers(name)
        self.next_attempt[name] = now
        self.restart_counts[name] += 1
        self._write_state()
        self._log(f"started {name} pid={child.pid}")

    def _schedule_retry(self, name: str, reason: str) -> None:
        self.failures[name] += 1
        delay = min(_MAX_BACKOFF_SECONDS, 2 ** min(self.failures[name], 5))
        self.next_attempt[name] = _tick() + delay
        self.last_exit[name] = {
            "reason": str(reason)[:240],
            "at": time.time(),
            "retry_in": delay,
        }
        self._log(f"{name} retry scheduled reason={reason}; retry_in={delay}s")
        self._write_state()

    def _stop_child(self, name: str) -> None:
        # The first pass after a sleep can stop a child it finds exited before any health check has
        # measured that sleep: measure the span up to the stop on its own, with the poll's allowance,
        # so none of that sleep passes for the stop's own 18 s of awake time.
        self._absorb_pause(_POLL_SECONDS, f"before stopping {name}")
        child = self.processes.pop(name, None)
        handle = self.handles.pop(name, None)
        if child is not None and child.poll() is None:
            try:
                if _is_windows_platform():
                    completed = subprocess.run(
                        ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=8,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if completed.returncode != 0:
                        self._log(
                            f"{name} process-tree stop returned {completed.returncode}; using direct fallback"
                        )
                else:
                    child.terminate()
                child.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    child.kill()
                    child.wait(timeout=5)
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        self.started_at.pop(name, None)
        self.exit_seen_at.pop(name, None)
        self._forget_answers(name)
        # Our own work: awake time up to its own limit, a pause beyond that (see _PAUSE_SECONDS).
        self._absorb_pause(_STOP_SECONDS, f"while stopping {name}")

    def _forget_answers(self, name: str) -> None:
        self.answered_at.pop(name, None)
        self.unanswered.pop(name, None)
        self.unanswered_since.pop(name, None)

    def _answered(self, name: str, now: float) -> None:
        """The current child answered a health check: its run of unanswered checks is over, and once
        it has answered for long enough the restart backoff forgets earlier failures."""
        self.unanswered.pop(name, None)
        self.unanswered_since.pop(name, None)
        answered_at = self.answered_at.setdefault(name, now)
        if self.failures[name] and now - answered_at >= _ANSWERING_SECONDS_BEFORE_BACKOFF_RESET:
            self._log(f"{name} answering for {now - answered_at:.0f}s; restart backoff reset")
            self.failures[name] = 0

    def _reap_dead(self, name: str) -> None:
        child = self.processes.get(name)
        if child is None or child.poll() is None:
            return
        returncode = child.poll()
        self._stop_child(name)
        self._schedule_retry(name, f"exited returncode={returncode}")

    def _watch_child(self, name: str, url: str) -> bool:
        """Check one supervised child's health. True: start nothing this pass (the endpoint answers,
        the live child still has time, or it was just stopped with its retry scheduled). False:
        there is no live child, so the caller may start one.

        A child that has never answered keeps the startup rule: stopped once it is
        _STARTUP_TIMEOUT_SECONDS old. A child that has answered is stopped only after
        _UNANSWERED_CHECKS_BEFORE_RESTART unanswered checks in a row spanning its
        _UNANSWERED_SECONDS_BEFORE_RESTART."""
        healthy = self._health_probe(name, url)
        # When that check ended: a pause after it (one that starts while a change of health is logged
        # included) is measured from there, once.
        now = self._last_measured()
        if healthy:
            self._answered(name, now)
            return True
        child = self.processes.get(name)
        if child is None or child.poll() is not None:
            return False
        if name not in self.answered_at:
            if now - self.started_at.setdefault(name, now) <= _STARTUP_TIMEOUT_SECONDS:
                return True
            self._stop_child(name)
            self._schedule_retry(name, "startup timeout")
            return True
        count = self.unanswered.get(name, 0) + 1
        self.unanswered[name] = count
        span = now - self.unanswered_since.setdefault(name, now)
        if count < _UNANSWERED_CHECKS_BEFORE_RESTART or span < _UNANSWERED_SECONDS_BEFORE_RESTART[name]:
            return True
        self._stop_child(name)
        self._schedule_retry(name, f"unresponsive: {count} health checks unanswered over {span:.0f}s")
        return True

    def _ensure_ollama(self) -> None:
        if self._watch_child("ollama", "http://127.0.0.1:11434/api/tags"):
            return
        self._start("ollama", [str(self.root / "ollama" / "ollama.exe"), "serve"], cwd=self.root)

    def _ensure_api(self) -> None:
        if self._watch_child("api", "http://127.0.0.1:11435/healthz"):
            return
        if _tick() < self.next_attempt["api"]:
            return  # backing off: no process scan and no start until the retry is due
        self._clear_stale_api_processes()
        # Our own work: awake time up to its own limit, a pause beyond that (see _PAUSE_SECONDS).
        self._absorb_pause(_STALE_API_CLEANUP_TIMEOUT_SECONDS, "during the stale-API scan")
        self._start(
            "api",
            [str(self.root / "python" / "python.exe"), str(self.root / "app" / "apps" / "vool_api_server.py"), "--port", "11435"],
            cwd=self.root / "app",
        )

    def _clear_stale_api_processes(self) -> None:
        """Stop only stale VOOL API Python processes before a bundle API start on Windows."""
        if not _is_windows_platform():
            return
        root = "".join(c + c if c in _POWERSHELL_SINGLE_QUOTES else c for c in str(self.root))
        supervisor_pid = os.getpid()
        command = (
            f"$r = '{root}'; "
            "$rootPrefix = $r.TrimEnd([char[]]@(92,47)) + [char]92; "
            "$script = [regex]::Escape($rootPrefix + 'app\\apps\\vool_api_server.py'); "
            f"$scriptArgument = '{_WINDOWS_SCRIPT_ARGUMENT_PREFIX}' + $script + '(?:\\x22|\\s|$)'; "
            f"$supervisorPid = {supervisor_pid}; "
            "$all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue); "
            "$protectedPids = New-Object 'System.Collections.Generic.HashSet[int]'; "
            "[void]$protectedPids.Add([int]$PID); "
            "$currentPid = [int]$supervisorPid; "
            "while ($currentPid -gt 0 -and $protectedPids.Add($currentPid)) { "
            "$current = $all | Where-Object { [int]$_.ProcessId -eq $currentPid } | Select-Object -First 1; "
            "if (-not $current) { break }; "
            "$currentPid = [int]$current.ParentProcessId "
            "}; "
            "$all | Where-Object { ($_.Name -ieq 'python.exe' -or $_.Name -ieq 'pythonw.exe') "
            "-and (-not $protectedPids.Contains([int]$_.ProcessId)) "
            "-and ([string]$_.CommandLine -match $scriptArgument) } | "
            "ForEach-Object { "
            "try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} "
            "}"
        )
        try:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    command,
                ],
                # The scan's output is never read, so it is never decoded. Text in a code page Python
                # cannot decode (a localized PowerShell message) would raise UnicodeDecodeError out of
                # run() on POSIX; on Windows, where the scan runs, subprocess's reader thread swallows
                # that error and loses the output.
                capture_output=True,
                check=False,
                timeout=_STALE_API_CLEANUP_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            self._log(f"stale api cleanup unavailable: {type(exc).__name__}: {exc}")
        except subprocess.TimeoutExpired:
            self._log(f"stale api cleanup gave up after {_STALE_API_CLEANUP_TIMEOUT_SECONDS:.0f}s; starting the API")

    def _ensure_window(self) -> None:
        """Open the native UI once the API is reachable; closing the UI is user intent."""
        if self.window_started:
            return
        healthy = self._checked("http://127.0.0.1:11435/healthz", "the window's health check")
        api = self.processes.get("api")
        if healthy and api is not None and api.poll() is None:
            # The API this supervisor started answered, as if to its own check: it is past the
            # startup rule (one late /healthz right after the window opens on it is no startup
            # timeout), and its run of unanswered checks starts over.
            self._answered("api", self._last_measured())
        if not healthy:
            return
        # This supervisor owns the API, so the window host attaches to it instead of trying to
        # own one: it refuses an occupied port in owned mode, and refuses any runtime when it
        # has no expected commit (a bundle has no .git to ask). It still checks that the API it
        # attaches to serves exactly this bundle's stamped commit and runtime profile, and -- when
        # this supervisor started the API -- that it is that exact process, which then vouches for
        # it through slow first-run health answers the way an owned child would.
        window_env = {"VOOL_NATIVE_REQUIRE_OWNED_RUNTIME": "0", "VOOL_EXPECTED_COMMIT": self._bundle_commit()}
        api = self.processes.get("api")
        if api is not None and api.poll() is None:
            window_env["VOOL_NATIVE_VOUCHING_PID"] = str(api.pid)
        self._start(
            "window",
            [str(self.root / "python" / "pythonw.exe"), str(self.root / "vool_window.py")],
            cwd=self.root,
            extra_env=window_env,
        )
        self.window_started = "window" in self.processes

    def _reap_window(self) -> None:
        """A window that closed because its runtime was lost (vool_window exit 3, e.g. the API was
        restarted) reopens once the API answers again. A window reopened that way that exits 1 soon
        after its start never attached, and is tried again with the restart backoff, a few times
        (_WINDOW_ATTACH_SECONDS, _WINDOW_REOPEN_RETRIES). Any other exit -- the user closing the
        window included -- stays closed until the user launches VOOL again."""
        child = self.processes.get("window")
        if child is None or child.poll() is None:
            return
        returncode = child.poll()
        # Its life in awake time, up to the first measured instant its exit was seen at (the last
        # one, if it exited after that); _stop_child forgets its start.
        started = self.started_at.get("window")
        exited = self.exit_seen_at.get("window", self._last_measured())
        lived = 0.0 if started is None else exited - started
        self._stop_child("window")
        self._log(f"window exited returncode={returncode}")
        if returncode == 3:
            self.window_started = False
            self._window_lost_runtime = True
            # Its watchdog runs only once it has attached: the retries of a reopen start over.
            self.failures["window"] = 0
        elif (
            returncode == 1
            and self._window_lost_runtime
            and lived <= _WINDOW_ATTACH_SECONDS
            and self.failures["window"] < _WINDOW_REOPEN_RETRIES
        ):
            # The reopened window's host made its one readiness check of the API, which had just
            # answered ours, got no answer, and exited 1 without showing a window: not the user's
            # close. It opens again once the API answers and its retry is due.
            self.window_started = False
            self._schedule_retry("window", "the reopened window did not attach to the API (returncode=1)")
        else:
            self._window_lost_runtime = False

    def _request_window_reopen(self) -> None:
        """A second launch while this home's supervisor runs asks that supervisor to show the
        window again, the way reopening the Mac app shows its hidden window. The request is a
        file in this home's own run folder, like the stop request: it reaches only the
        supervisor that holds this home's lease, and no other program."""
        with contextlib.suppress(OSError):
            self.window_reopen_path.write_text("reopen\n", encoding="utf-8")

    def _consume_window_reopen(self) -> None:
        """Honor a reopen request: a window the user closed opens again once the API answers. A
        window that is still open is left as it is, unless it exits within the grace period --
        it was closing when the request came."""
        now = time.monotonic()
        try:
            self.window_reopen_path.unlink()
        except OSError:
            pass
        else:
            self._log("window reopen requested")
            self.window_reopen_until = now + _REOPEN_GRACE_SECONDS
        if "window" not in self.processes and now < self.window_reopen_until:
            self.window_started = False
            self.window_reopen_until = 0.0

    def _write_state(self) -> None:
        wall, tick = time.time(), _tick()
        payload = {
            "schema": "vool.bundle_supervisor.v1",
            "root": str(self.root),
            "supervisor_pid": os.getpid(),
            "started_at": self.supervisor_started_at,
            "updated_at": wall,
            "pids": {name: child.pid for name, child in self.processes.items() if child.poll() is None},
            "model": self._model(),
            "health": self.health,
            "failures": self.failures,
            "restart_counts": self.restart_counts,
            # Kept on _tick(); written as wall-clock epoch seconds (0.0: never scheduled) for whoever
            # reads the file.
            "next_attempt": {name: wall + at - tick if at else 0.0 for name, at in self.next_attempt.items()},
            "last_exit": self.last_exit,
        }
        with contextlib.suppress(OSError):
            self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        # Our own work, expected to be quick: awake time up to that, a pause beyond (see _PAUSE_SECONDS).
        # Measured from the last measured instant, so the bookkeeping since then is measured too.
        self._absorb_pause(0.0, "while writing the state file")

    def run(self, *, once: bool = False) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if not self._acquire_lease():
            self._request_window_reopen()
            self._log("supervisor already running; window reopen requested")
            return 0
        self._prepare()
        self.stop_path.unlink(missing_ok=True)
        self.window_reopen_path.unlink(missing_ok=True)
        self._log("supervisor started")
        try:
            while not self.stop_path.exists():
                self._reap_dead("ollama")
                self._reap_dead("api")
                self._ensure_ollama()
                self._ensure_api()
                self._reap_window()
                self._consume_window_reopen()
                self._ensure_window()
                self._write_state()
                if once:
                    break
                time.sleep(_POLL_SECONDS)
        finally:
            self._stop_child("window")
            self._stop_child("api")
            self._stop_child("ollama")
            self._write_model_status("stopped", "Bundle supervisor stopped.")
            self.state_path.unlink(missing_ok=True)
            self._log("supervisor stopped")
            self._release_lease()
        return 0


def main() -> int:
    # The launcher sets VOOL_HOME; a direct run (`--stop` by hand, or an installer step) does not,
    # and the runtime home then fell back to <app>\app\.vool_local -- the preflight wrote its record
    # into the program folder, which uninstall deletes. Resolve the same home the launcher does.
    os.environ.setdefault("VOOL_HOME", str(native_runtime_home()))
    # C15: the ONE bounded unattended preflight — the .app supervisor is an unattended boot.
    from core.unattended_preflight import preflight

    preflight("installer.bundle.bundle_supervisor")
    parser = argparse.ArgumentParser(prog="vool-bundle-supervisor")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.stop:
        home = native_runtime_home()
        run_dir = home / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stop").write_text("stop\n", encoding="utf-8")
        return 0
    return BundleSupervisor(root).run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
