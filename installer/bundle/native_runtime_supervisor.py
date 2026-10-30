"""Owned runtime lifecycle for the native VOOL/VOOL window.

The macOS app launcher ``exec``s the native host; this object then owns any API process it starts.
It will attach only to a runtime whose stamped Git identity matches the exact candidate, rejects an
unknown occupant on the canonical port, gates the window on real ``/healthz`` readiness, and tears
its child process group down when the native event loop exits.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.process_liveness import pid_alive, system_taskkill, system_tool
from core.runtime_paths import active_vool_home, runtime_home_identity


def _windows_cmd_starter(command: list[str]) -> str | None:
    """The .bat a Windows wrapper command runs through ``cmd /d /s /c``, or None for any other command."""
    if len(command) == 5 and [part.lower() for part in command[1:4]] == ["/d", "/s", "/c"]:
        return command[4]
    return None


def _windows_job() -> Any:
    """A Job object with KILL_ON_JOB_CLOSE whose only handle this process holds (not inherited)."""
    from core.windows_broker.process import _WindowsJob

    return _WindowsJob()


class NativeRuntimeError(RuntimeError):
    """The exact native runtime could not be established safely."""


class _NoHealthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sha_matches(expected: str, actual: str) -> bool:
    expected = str(expected or "").strip().lower()
    actual = str(actual or "").strip().lower()
    if not expected or not actual:
        return False
    if not all(char in "0123456789abcdef" for char in expected + actual):
        return False
    return expected.startswith(actual) or actual.startswith(expected)


class NativeRuntimeSupervisor:
    """Start, identify, monitor, and stop the API used by one native-window lifetime."""

    def __init__(
        self,
        project_root: Path,
        command: list[str],
        *,
        expected_sha: str,
        health_url: str = "http://127.0.0.1:11435/healthz",
        env: dict[str, str] | None = None,
        require_owned: bool = True,
        startup_timeout: float = 90.0,
        poll_interval: float = 0.25,
        unhealthy_grace: float = 30.0,
        vouching_pid: int | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        job_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.command = [str(part) for part in command]
        self.expected_sha = str(expected_sha or "").strip().lower()
        self.health_url = str(health_url)
        self._validate_health_origin()
        self.env = dict(os.environ if env is None else env)
        self.runtime_home_id = runtime_home_identity(self.env)
        self.require_owned = bool(require_owned)
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.unhealthy_grace = max(0.0, float(unhealthy_grace))
        # The exact API process an outer supervisor started and vouches for (the Windows bundle
        # supervisor). An attachment then binds to that process, and it vouches like an owned child.
        self.vouching_pid = int(vouching_pid) if vouching_pid and int(vouching_pid) > 1 else None
        self._popen = popen
        self._sleep = sleep
        self._monotonic = monotonic
        self._job_factory = job_factory or _windows_job
        # Windows: the Job object the owned tree lives in. When this process ends, however it ends,
        # Windows closes the handle and kills the tree (G4 orphan check, Steve, 177621c).
        self._job: Any | None = None
        self._unhealthy_since: float | None = None
        self.process: Any | None = None
        self.log_handle: Any | None = None
        self.attached_existing = False
        self.ready_identity: dict[str, Any] = {}

    @classmethod
    def from_environment(cls, *, module_path: str | Path) -> NativeRuntimeSupervisor:
        module_root = Path(module_path).resolve().parents[2]
        root = Path(os.environ.get("VOOL_PROJECT_ROOT") or module_root).expanduser().resolve()
        mode = str(os.environ.get("VOOL_RUNTIME_MODE") or "wrapper").strip().lower()
        if mode == "self-contained":
            command = [sys.executable, "-m", "apps.vool_api_server"]
        elif sys.platform == "win32":
            # The Windows bundle ships Start_VOOL.bat; there is no bash. A missing .bat is refused
            # by ensure_ready before anything is spawned.
            starter = root / "Start_VOOL.bat"
            # System32's cmd.exe only, never ComSpec or a cmd found on PATH (#97). When it can't be
            # trusted the command keeps an empty interpreter and ensure_ready refuses to spawn.
            try:
                cmd = system_tool("cmd")
            except OSError:
                cmd = ""
            command = [cmd, "/d", "/s", "/c", str(starter)]
        else:
            starter = root / "Start_VOOL.sh"
            command = ["bash", str(starter)]
        expected = str(os.environ.get("VOOL_EXPECTED_COMMIT") or "").strip()
        if not expected:
            with contextlib.suppress(Exception):
                expected = subprocess.check_output(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                ).strip()
        origin = str(os.environ.get("VOOL_NATIVE_API_URL") or "http://127.0.0.1:11435").rstrip("/")
        if mode == "self-contained":
            # The owned daemon listens where the window will look. Without this the self-contained
            # command took the server's default port whatever the origin said, so a second bundle
            # instance (an acceptance run beside an operator's live window) attached to -- or
            # stopped -- the runtime on 11435 instead of owning its own.
            port = urllib.parse.urlsplit(origin).port
            if port:
                command = [*command, "--port", str(port)]
        timeout = float(os.environ.get("VOOL_NATIVE_STARTUP_TIMEOUT") or 300)
        grace = float(os.environ.get("VOOL_NATIVE_UNHEALTHY_GRACE") or 30)
        require_owned = str(os.environ.get("VOOL_NATIVE_REQUIRE_OWNED_RUNTIME") or "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        vouching = str(os.environ.get("VOOL_NATIVE_VOUCHING_PID") or "").strip()
        return cls(
            root,
            command,
            expected_sha=expected,
            health_url=f"{origin}/healthz",
            require_owned=require_owned,
            startup_timeout=timeout,
            unhealthy_grace=grace,
            vouching_pid=int(vouching) if vouching.isdigit() else None,
        )

    @property
    def owns_runtime(self) -> bool:
        return self.process is not None


    def _validate_health_origin(self) -> None:
        try:
            parts = urllib.parse.urlsplit(self.health_url)
            host = parts.hostname or ""
            port = parts.port
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
            valid = (
                parts.scheme in {"http", "https"}
                and parts.username is None and parts.password is None
                and not parts.query and not parts.fragment
                and (port is None or 0 < port <= 65535)
                and loopback
            )
        except ValueError:
            valid = False
        if not valid:
            raise NativeRuntimeError("native health origin must be a direct loopback HTTP endpoint without userinfo")

    def _health(self) -> dict[str, Any] | None:
        self._validate_health_origin()
        try:
            request = urllib.request.Request(self.health_url, headers={"Accept": "application/json"})
            # A local runtime cannot be identified through an ambient HTTP proxy
            # or a redirect. An HTTP refusal proves an occupant, not a free port.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoHealthRedirect())
            with opener.open(request, timeout=1.5) as response:
                if int(getattr(response, "status", 200)) != 200:
                    return {}
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return {}
        except (ValueError, TypeError):
            return {}
        except (OSError, urllib.error.URLError):
            return None
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _identity(payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            # /api/runtime/version may itself be the stamp; accept that form for diagnostics.
            runtime = payload if "commit" in payload or "commit_full" in payload else {}
        return dict(runtime)

    def _identity_matches(self, payload: dict[str, Any] | None) -> bool:
        identity = self._identity(payload)
        actual = str(identity.get("commit_full") or identity.get("commit") or "").strip()
        return (
            _sha_matches(self.expected_sha, actual)
            and self._recognized_vool(payload)
            and identity.get("runtime_home_id") == self.runtime_home_id
        )

    @staticmethod
    def _recognized_vool(payload: dict[str, Any] | None) -> bool:
        """Recognize the stable VOOL health schema, never the user-chosen display name."""
        if not isinstance(payload, dict):
            return False
        runtime = payload.get("runtime")
        return (
            payload.get("ok") is True
            and isinstance(runtime, dict)
            and isinstance(runtime.get("pid"), int)
            and int(runtime["pid"]) > 1
            and isinstance(runtime.get("protocol_version"), int)
            and int(runtime["protocol_version"]) >= 1
            and str(runtime.get("workstation_version") or "").startswith("vool-workstation-")
            and bool(str(runtime.get("release_version") or "").strip())
        )

    def _owned_identity_matches(self, payload: dict[str, Any] | None) -> bool:
        if self.process is None or not self._identity_matches(payload):
            return False
        pid = self._identity(payload).get("pid")
        if pid == self.process.pid:
            return True
        # The Windows wrapper's own child is cmd.exe; the API it starts is that cmd's descendant.
        return _windows_cmd_starter(self.command) is not None and pid in self._descendant_pids()

    def _descendant_pids(self) -> set[int]:
        """Pids under the owned child (psutil drops reused pids); empty, never a guess, on any failure."""
        try:
            import psutil

            return {int(proc.pid) for proc in psutil.Process(self.process.pid).children(recursive=True)}
        except Exception:
            return set()

    def _spawn_command(self) -> list[str] | str:
        starter = _windows_cmd_starter(self.command)
        if starter is None or os.name != "nt":
            return self.command
        # cmd /s strips only the outer pair of quotes, so the inner pair reaches the .bat path intact
        # even with ( ) & ^ in it. list2cmdline would escape the inner quotes instead.
        return f'"{self.command[0]}" /d /s /c ""{starter}""'

    def ensure_ready(self) -> dict[str, Any]:
        """Return the exact runtime stamp, or raise before any native window is created."""
        if not self.expected_sha:
            raise NativeRuntimeError("expected source SHA is unavailable; refusing an unidentifiable native build")
        existing = self._health()
        if existing is not None:
            if not self._recognized_vool(existing):
                raise NativeRuntimeError("API port is occupied by an unidentifiable process; no process was stopped")
            if self._identity(existing).get("runtime_home_id") != self.runtime_home_id:
                raise NativeRuntimeError("API port belongs to another or unproved runtime profile; no process was stopped")
            if self._identity_matches(existing) and not self.require_owned:
                if self.vouching_pid is not None and self._identity(existing).get("pid") != self.vouching_pid:
                    raise NativeRuntimeError(
                        "API port is served by a process other than the one the supervisor started; "
                        "no process was stopped"
                    )
                self.attached_existing = True
                self.ready_identity = self._identity(existing)
                return dict(self.ready_identity)
            # A recognizable stamp cannot prove this host created that process.
            # Default ownership never replaces an occupant, even in the same profile.
            raise NativeRuntimeError(
                "API port is already occupied; quit that runtime or select another API port. No process was stopped"
            )
        if not self.command:
            raise NativeRuntimeError("native runtime start command is empty")
        if self.command[0] == "bash" and len(self.command) > 1 and not Path(self.command[1]).is_file():
            raise NativeRuntimeError(f"runtime starter is missing: {self.command[1]}")
        cmd_starter = _windows_cmd_starter(self.command)
        if cmd_starter is not None:
            if not self.command[0]:
                raise NativeRuntimeError("Windows' own cmd.exe in System32 can't be trusted here; nothing was started")
            if not Path(cmd_starter).is_file():
                raise NativeRuntimeError(f"runtime starter is missing: {cmd_starter}")
            if '"' in cmd_starter or "%" in cmd_starter:
                # cmd expands %...% even inside quotes; refuse rather than run a different path.
                raise NativeRuntimeError(f"runtime starter path cannot be passed to cmd safely: {cmd_starter}")

        log_dir = active_vool_home(self.env) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_handle = (log_dir / "native-runtime-child.log").open("ab")
        deadline = time.monotonic() + self.startup_timeout
        # The child can wait for human authorization without racing a shorter
        # independent timeout. Leave time for binding the API after authorization.
        child_env = dict(self.env)
        child_env["VOOL_NATIVE_AUTHORIZATION_DEADLINE"] = str(deadline - min(5.0, self.startup_timeout / 10))
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_root),
            "env": child_env,
            "stdin": subprocess.DEVNULL,
            "stdout": self.log_handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
            try:
                self._job = self._job_factory()
            except Exception as exc:
                self._close_log()
                raise NativeRuntimeError(
                    f"the Windows job that ties the runtime to this window could not be created "
                    f"({type(exc).__name__}: {exc}); nothing was started"
                ) from exc
        try:
            self.process = self._popen(self._spawn_command(), **kwargs)
        except Exception as exc:
            self._close_job()
            self._close_log()
            raise NativeRuntimeError(f"runtime spawn failed: {type(exc).__name__}: {exc}") from exc
        if self._job is not None:
            try:
                self._job.assign(self.process.pid)
                # Whatever cmd started before the assignment is not in the job yet; its later
                # children are, because a job's processes start their children inside it.
                for pid in sorted(self._descendant_pids()):
                    self._job.assign(pid)
            except Exception as exc:
                self.shutdown()
                raise NativeRuntimeError(
                    f"the runtime could not be tied to this window ({type(exc).__name__}: {exc}); it was stopped"
                ) from exc

        while time.monotonic() < deadline:
            returncode = self.process.poll()
            if returncode is not None:
                self.shutdown()
                raise NativeRuntimeError(f"runtime child exited before readiness (returncode={returncode})")
            payload = self._health()
            if payload is not None:
                if not self._owned_identity_matches(payload):
                    actual = self._identity(payload).get("commit_full") or self._identity(payload).get("commit") or "unknown"
                    self.shutdown()
                    raise NativeRuntimeError(
                        f"spawned runtime identity mismatch: expected own PID, profile and {self.expected_sha}; served {actual}"
                    )
                self.ready_identity = self._identity(payload)
                return dict(self.ready_identity)
            self._sleep(self.poll_interval)
        self.shutdown()
        raise NativeRuntimeError(f"runtime readiness timed out after {self.startup_timeout:.1f}s")

    def assert_alive(self) -> None:
        """Raise when an owned child has died, the serving identity changed, or health stays gone.

        A dead owned child or a different identity is fatal at once. A health probe that fails while
        the owned child is still running is not, on its own: a runtime busy with first-run work (a
        background model pull on a slower Intel Mac measured a 2.3 s /healthz against the 1.5 s
        probe) answered late once and the whole app closed under the user. Such failures are
        tolerated for ``unhealthy_grace`` seconds of consecutive failure, then fatal. Without an
        owned child to vouch for the runtime, an unreachable endpoint is fatal at once. An attachment
        to the process an outer supervisor started (``vouching_pid``) is vouched for by that exact
        live process the same way: its exit is fatal at once, and so is any other pid answering.
        """
        if self.process is not None and self.process.poll() is not None:
            raise NativeRuntimeError(f"owned runtime child exited (returncode={self.process.returncode})")
        vouched = self.process is None and self.vouching_pid is not None
        if vouched and not pid_alive(self.vouching_pid):
            raise NativeRuntimeError(f"supervised runtime process exited (pid={self.vouching_pid})")
        payload = self._health()
        if payload is None:
            now = self._monotonic()
            if self._unhealthy_since is None:
                self._unhealthy_since = now
            if (self.process is None and not vouched) or now - self._unhealthy_since >= self.unhealthy_grace:
                raise NativeRuntimeError("runtime health endpoint is unavailable")
            return
        self._unhealthy_since = None
        if self.process is not None:
            same_runtime = self._owned_identity_matches(payload)
        else:
            same_runtime = self._identity_matches(payload) and (
                not vouched or self._identity(payload).get("pid") == self.vouching_pid
            )
        if not same_runtime:
            raise NativeRuntimeError("runtime identity changed while the native app was open")

    def _close_log(self) -> None:
        handle, self.log_handle = self.log_handle, None
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()

    def shutdown(self) -> None:
        """Tear down only the process group this native host actually created."""
        child, self.process = self.process, None
        if child is not None and child.poll() is None:
            with contextlib.suppress(Exception):
                if os.name == "posix":
                    os.killpg(os.getpgid(child.pid), signal.SIGTERM)
                elif os.name == "nt":
                    # Windows has no group signalling: take the child's whole tree
                    # down through the recorded PID while the root still runs (once
                    # it has exited, taskkill /T can't walk to the helpers it
                    # started). Nothing outside that tree is ever touched; if the
                    # tree kill fails, the wait below falls back to killing the child.
                    subprocess.run(
                        (system_taskkill(), "/PID", str(child.pid), "/T", "/F"),
                        capture_output=True, check=False, timeout=8,
                    )
                else:
                    child.terminate()
            try:
                child.wait(timeout=8)
            except Exception:
                with contextlib.suppress(Exception):
                    if os.name == "posix":
                        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
                    else:
                        child.kill()
                with contextlib.suppress(Exception):
                    child.wait(timeout=3)
        # Closing the job ends anything of the tree the kill above missed.
        self._close_job()
        self._close_log()

    def _close_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            with contextlib.suppress(Exception):
                job.close()

    close = shutdown
