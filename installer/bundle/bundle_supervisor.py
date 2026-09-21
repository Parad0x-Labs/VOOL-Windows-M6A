"""Windows bundle supervisor for the Ollama and VOOL API child processes."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_POLL_SECONDS = 2.0
_STARTUP_TIMEOUT_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 30.0


class BundleSupervisor:
    def __init__(self, root: Path, *, env: dict[str, str] | None = None) -> None:
        self.root = root.resolve()
        self.env = dict(os.environ if env is None else env)
        local_app_data = Path(self.env.get("LOCALAPPDATA") or Path.home())
        self.home = Path(self.env.get("VOOL_HOME") or local_app_data / "VOOL").expanduser()
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
        self.supervisor_started_at = time.time()
        self._lock_handle: Any | None = None
        self._mutex_handle: Any | None = None
        self.window_started = False
        self.window_closed = False
        self._control_secret: bytes | None = None

    def _manifest(self) -> dict[str, Any]:
        path = self.root / "bundle_manifest.json"
        try:
            # Windows PowerShell 5 emits UTF-8 JSON with a BOM by default.
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

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
        self.env["VOOL_WINDOWS_BUNDLE"] = "1"
        self.env["VOOL_DISABLE_MESH_DAEMON"] = "1"
        self.env["VOOL_PUBLIC_HIVE_ENABLED"] = "0"
        self.env["VOOL_BUNDLE_MANIFEST"] = str(self.root / "bundle_manifest.json")
        self.env["VOOL_HOME"] = str(self.home)
        self.env["VOOL_PROJECT_ROOT"] = str(self.root / "app")
        self.env["VOOL_RUNTIME_MODE"] = "self-contained"
        self.env["VOOL_NATIVE_REQUIRE_OWNED_RUNTIME"] = "0"
        self.env["VOOL_NATIVE_ATTACH_ONLY"] = "1"
        self.env["VOOL_NATIVE_API_URL"] = "http://127.0.0.1:11435"
        self.env.pop("VOOL_EXPECTED_COMMIT", None)
        commit = str(self._manifest().get("source_commit_full") or "").strip().lower()
        if len(commit) == 40 and all(char in "0123456789abcdef" for char in commit):
            self.env["VOOL_EXPECTED_COMMIT"] = commit
        model_store = self._model_store()
        self.env["OLLAMA_MODELS"] = str(model_store)
        model = self._model()
        if model:
            self.env["VOOL_BUNDLE_MODEL"] = model
            self.env["VOOL_OLLAMA_MODEL"] = model
        model_store.mkdir(parents=True, exist_ok=True)
        self._write_model_status("delegated_to_api", "The API owns the non-blocking model pull.")

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
        mutex_handle = None
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VOOL_BUNDLE_SUPERVISOR")
                if not mutex_handle or ctypes.windll.kernel32.GetLastError() == 183:
                    raise OSError("another VOOL bundle supervisor owns the installer mutex")
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii", errors="replace"))
            handle.flush()
        except (AttributeError, ImportError, OSError):
            if mutex_handle:
                ctypes.windll.kernel32.CloseHandle(mutex_handle)
            handle.close()
            return False
        self._lock_handle = handle
        self._mutex_handle = mutex_handle
        return True

    def _release_lease(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        mutex_handle = self._mutex_handle
        self._mutex_handle = None
        if mutex_handle:
            with contextlib.suppress(Exception):
                ctypes.windll.kernel32.CloseHandle(mutex_handle)
        if handle is None:
            return
        with contextlib.suppress(ImportError, OSError):
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()

    def _request_window_reopen(self) -> None:
        """Ask the owned supervisor to ensure the native window is visible again."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.window_reopen_path.write_text(f"{os.getpid()}\n", encoding="ascii")
        except OSError:
            # A duplicate launch must never turn into an unbounded retry or a second
            # supervisor. The existing instance remains the single owner of the runtime.
            pass

    def _consume_window_reopen(self) -> bool:
        """Consume a same-user relaunch request without starting a second supervisor."""
        try:
            self.window_reopen_path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False

        self._log("window reopen requested")
        self.window_closed = False
        if "window" in self.processes:
            self._stop_child("window")
        self.window_started = False
        self.next_attempt["window"] = 0.0
        return True

    def _healthy(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                return 200 <= int(response.status) < 400
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def _health_probe(self, name: str, url: str) -> bool:
        healthy = self._owned_api_healthy() if name == "api" else self._healthy(url)
        previous = self.health.get(name, {}).get("healthy")
        self.health[name] = {
            "url": url,
            "healthy": healthy,
            "checked_at": time.time(),
        }
        if previous is None or previous != healthy:
            self._log(f"health {name}={'healthy' if healthy else 'unhealthy'} url={url}")
        return healthy

    @staticmethod
    def _api_listener_pid() -> int | None:
        """Use the OS TCP table, not a server-supplied PID, to identify the loopback listener."""
        try:
            import psutil

            for connection in psutil.net_connections(kind="tcp"):
                address = connection.laddr
                if (
                    connection.status == psutil.CONN_LISTEN
                    and address
                    and address.port == 11435
                    and address.ip in {"127.0.0.1", "::1"}
                ):
                    return connection.pid
        except (ImportError, OSError, AttributeError):
            pass
        return None

    def _owned_api_healthy(self) -> bool:
        child = self.processes.get("api")
        expected = str(self.env.get("VOOL_EXPECTED_COMMIT") or "")
        if child is None or child.poll() is not None or not expected:
            return False
        if self._api_listener_pid() != child.pid:
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:11435/healthz", timeout=1.5) as response:
                payload = json.load(response)
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            return False
        if not isinstance(payload, dict):
            return False
        runtime = payload.get("runtime")
        return bool(
            payload.get("ok") is True
            and isinstance(runtime, dict)
            and runtime.get("commit_full") == expected
            and runtime.get("pid") == child.pid
        )

    def _child_env(self) -> dict[str, str]:
        env = dict(self.env)
        app = str(self.root / "app")
        env["PYTHONPATH"] = app + os.pathsep + str(env.get("PYTHONPATH") or "")
        return env

    def _start(self, name: str, argv: list[str], *, cwd: Path) -> None:
        now = time.time()
        if now < self.next_attempt[name]:
            return
        log_path = self.log_dir / f"{name}.log"
        handle = log_path.open("ab")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        control_secret: bytes | None = None
        if self.env.get("VOOL_WINDOWS_BUNDLE") == "1" and name in {"api", "window"}:
            if name == "api":
                self._control_secret = secrets.token_bytes(32)
            control_secret = self._control_secret
            if control_secret is None:
                handle.close()
                self._schedule_retry(name, "native control channel unavailable")
                return
        child_env = self._child_env()
        if control_secret is not None:
            # The marker names an inherited anonymous stdin pipe, never the secret.
            child_env["VOOL_NATIVE_CONTROL_STDIN"] = "1"
        child: subprocess.Popen[Any] | None = None
        try:
            child = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=child_env,
                stdin=subprocess.PIPE if control_secret is not None else subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            if control_secret is not None:
                pipe = child.stdin
                if pipe is None:
                    raise OSError("native control pipe was not created")
                try:
                    pipe.write(control_secret)
                    pipe.flush()
                finally:
                    pipe.close()
        except Exception as exc:
            if child is not None:
                with contextlib.suppress(Exception):
                    child.kill()
                    child.wait(timeout=3)
            handle.close()
            if name == "api":
                self._control_secret = None
            self._schedule_retry(name, f"failed to start: {type(exc).__name__}: {exc}")
            return
        self.processes[name] = child
        self.handles[name] = handle
        self.started_at[name] = now
        self.next_attempt[name] = now
        self.restart_counts[name] += 1
        self._write_state()
        self._log(f"started {name} pid={child.pid}")

    def _schedule_retry(self, name: str, reason: str) -> None:
        self.failures[name] += 1
        delay = min(_MAX_BACKOFF_SECONDS, 2 ** min(self.failures[name], 5))
        self.next_attempt[name] = time.time() + delay
        self.last_exit[name] = {
            "reason": str(reason)[:240],
            "at": time.time(),
            "retry_in": delay,
        }
        self._log(f"{name} retry scheduled reason={reason}; retry_in={delay}s")
        self._write_state()

    def _stop_child(self, name: str) -> None:
        if name == "api":
            # A replacement API receives a new secret. Stop the old window first,
            # so it cannot keep signing requests against a changed runtime.
            self._stop_child("window")
            self.window_started = False
            self._control_secret = None
        child = self.processes.pop(name, None)
        handle = self.handles.pop(name, None)
        if child is not None and child.poll() is None:
            try:
                if os.name == "nt":
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

    def _reap_dead(self, name: str) -> None:
        child = self.processes.get(name)
        if child is None or child.poll() is None:
            return
        returncode = child.poll()
        self._stop_child(name)
        if name == "window":
            if returncode == 0:
                self.window_closed = True
                self._log("native window closed; stopping owned bundle services")
                return
            self.window_started = False
        self._schedule_retry(name, f"exited returncode={returncode}")

    def _ensure_ollama(self) -> None:
        if self._health_probe("ollama", "http://127.0.0.1:11434/api/tags"):
            return
        child = self.processes.get("ollama")
        if child is not None and child.poll() is None:
            if time.time() - self.started_at.get("ollama", time.time()) <= _STARTUP_TIMEOUT_SECONDS:
                return
            self._stop_child("ollama")
            self._schedule_retry("ollama", "startup timeout")
            return
        self._start("ollama", [str(self.root / "ollama" / "ollama.exe"), "serve"], cwd=self.root)

    def _ensure_api(self) -> None:
        if self._health_probe("api", "http://127.0.0.1:11435/healthz"):
            return
        child = self.processes.get("api")
        if child is not None and child.poll() is None:
            if time.time() - self.started_at.get("api", time.time()) <= _STARTUP_TIMEOUT_SECONDS:
                return
            self._stop_child("api")
            self._schedule_retry("api", "startup timeout")
            return
        listener_pid = self._api_listener_pid()
        if listener_pid is not None:
            self._log(f"API port 11435 is held by non-owned pid={listener_pid}; refusing to replace it")
            return
        # The embedded CPython runtime is isolated by its ``python312._pth``
        # file and ignores the ambient PYTHONPATH. Bootstrap the API with its
        # app root explicitly so ``core`` imports work from an installed bundle.
        api_bootstrap = (
            "import runpy,sys; "
            "app_root=sys.argv[1]; script=sys.argv[2]; "
            "sys.path.insert(0,app_root); "
            "sys.argv=[script,*sys.argv[3:]]; "
            "runpy.run_path(script,run_name='__main__')"
        )
        self._start(
            "api",
            [
                str(self.root / "python" / "python.exe"),
                "-c",
                api_bootstrap,
                str(self.root / "app"),
                str(self.root / "app" / "apps" / "vool_api_server.py"),
                "--port",
                "11435",
            ],
            cwd=self.root / "app",
        )

    def _ensure_window(self) -> None:
        """Open the native UI once the API is reachable; closing the UI is user intent."""
        if self.window_started or not self._owned_api_healthy():
            return
        native_host = self.root / "native" / "VOOL.exe"
        argv = (
            [str(native_host)]
            if os.name == "nt" and native_host.is_file()
            else [str(self.root / "python" / "pythonw.exe"), str(self.root / "vool_window.py")]
        )
        self._start(
            "window",
            argv,
            cwd=self.root,
        )
        self.window_started = "window" in self.processes

    def _write_state(self) -> None:
        payload = {
            "schema": "vool.bundle_supervisor.v1",
            "root": str(self.root),
            "supervisor_pid": os.getpid(),
            "started_at": self.supervisor_started_at,
            "updated_at": time.time(),
            "pids": {name: child.pid for name, child in self.processes.items() if child.poll() is None},
            "model": self._model(),
            "health": self.health,
            "failures": self.failures,
            "restart_counts": self.restart_counts,
            "next_attempt": self.next_attempt,
            "last_exit": self.last_exit,
        }
        with contextlib.suppress(OSError):
            self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def run(self, *, once: bool = False) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if not self._acquire_lease():
            self._request_window_reopen()
            self._log("supervisor already running; window reopen requested")
            return 0
        self._prepare()
        self.stop_path.unlink(missing_ok=True)
        self._log("supervisor started")
        try:
            while not self.stop_path.exists():
                self._consume_window_reopen()
                self._reap_dead("ollama")
                self._reap_dead("api")
                self._reap_dead("window")
                if self.window_closed:
                    break
                self._ensure_ollama()
                self._ensure_api()
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
    parser = argparse.ArgumentParser(prog="vool-bundle-supervisor")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    # The installed bundle launches this script from the bundle root. Keep the
    # application package importable without relying on the developer
    # checkout's PYTHONPATH.
    import sys

    app_root = str(root / "app")
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    # C15: the ONE bounded unattended preflight — the .app supervisor is an unattended boot.
    from core.unattended_preflight import preflight

    preflight("installer.bundle.bundle_supervisor")
    if args.stop:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        home = Path(os.environ.get("VOOL_HOME") or local_app_data / "VOOL")
        run_dir = home / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stop").write_text("stop\n", encoding="utf-8")
        return 0
    return BundleSupervisor(root).run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
