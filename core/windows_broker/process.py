"""Start and stop the Windows broker with the VOOL API (phase B).

The API starts ``VoolBroker.exe`` as its own child inside a Job object it holds with
KILL_ON_JOB_CLOSE, so the broker cannot outlive the API (docs/WINDOWS_BROKER.md section 2).
Nothing is routed through the broker yet: a broker that is missing, refused or slow is reported
in the log and VOOL runs exactly as it did without it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How long the host waits for the broker to take its bootstrap and answer with its start line.
START_TIMEOUT_SECONDS = 10.0
_FLAGGED_ENV = "VOOL_WINDOWS_DISPOSABLE_WORKSPACES"


@dataclass(frozen=True)
class BrokerStatus:
    running: bool
    #: Empty while running; otherwise why not: not_windows, not_found, start_failed,
    #: start_timeout, bad_start_line, exited:<code>, refused:<code>, stopped.
    reason: str
    pipe: str = ""
    pid: int = 0


class _Job(Protocol):
    def assign(self, pid: int) -> None: ...

    def close(self) -> None: ...


def _is_windows() -> bool:
    return os.name == "nt"


def broker_executable(project_root: Path) -> Path | None:
    """The broker beside the installed app ({app}\\broker), or the one a checkout published."""
    for candidate in (
        project_root.parent / "broker" / "VoolBroker.exe",
        project_root / "windows" / "broker" / "publish" / "VoolBroker.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


class BrokerHost:
    """One broker for the life of the API: start() once, stop() at exit (safe to repeat)."""

    def __init__(
        self,
        executable: Path,
        flagged: list[str],
        journal_dir: Path,
        *,
        timeout: float = START_TIMEOUT_SECONDS,
        popen: Callable[..., Any] = subprocess.Popen,
        job_factory: Callable[[], _Job] | None = None,
        api_identity: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self.executable = executable
        self.flagged = list(flagged)
        self.journal_dir = journal_dir
        self.timeout = timeout
        self.secret = b""
        self.status = BrokerStatus(False, "not_started")
        self._popen = popen
        self._job_factory = job_factory or _WindowsJob
        self._api_identity = api_identity or _current_process_identity
        self._process: Any = None
        self._job: _Job | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> BrokerStatus:
        with self._lock:
            self.status = self._start()
        if self.status.running:
            logger.info("Windows broker started: pid %s, pipe %s", self.status.pid, self.status.pipe)
        else:
            logger.warning("Windows broker not running (%s); VOOL continues without it", self.status.reason)
        return self.status

    def stop(self) -> None:
        # Set before taking the lock, so a start still waiting on the broker gives up at once.
        self._stopping.set()
        with self._lock:
            if self._process is None and self._job is None:
                return
            self._release()
            self.status = BrokerStatus(False, "stopped")

    def _close_job_for_tests(self) -> None:
        with self._lock:
            if self._job is not None:
                self._job.close()
                self._job = None

    def _start(self) -> BrokerStatus:
        if self._stopping.is_set():
            return BrokerStatus(False, "stopped")
        try:
            self._job = self._job_factory()
            self._process = self._popen(
                [str(self.executable)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # The broker reads its bootstrap before it does anything, so it is in the job first.
            self._job.assign(self._process.pid)
            self.secret = secrets.token_bytes(32)
            api_pid, api_creation_time = self._api_identity()
            bootstrap = {
                "secret": self.secret.hex(),
                "api_pid": api_pid,
                "api_creation_time": api_creation_time,
                "flagged": self.flagged,
                "journal_dir": str(self.journal_dir),
            }
            payload = json.dumps(bootstrap, separators=(",", ":")).encode("utf-8")
        except Exception:
            logger.exception("Windows broker could not be launched")
            self._release()
            return BrokerStatus(False, "start_failed")

        line = self._exchange(struct.pack("<I", len(payload)) + payload)
        if line is None:
            self._release()
            return BrokerStatus(False, "stopped" if self._stopping.is_set() else "start_timeout")
        if not line:
            try:
                code: object = self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = "unknown"
            self._release()
            return BrokerStatus(False, f"exited:{code}")
        try:
            ready = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            ready = None
        if isinstance(ready, dict) and isinstance(ready.get("error"), dict):
            code = str(ready["error"].get("code", "unknown"))
            self._release()
            return BrokerStatus(False, f"refused:{code}")
        if not isinstance(ready, dict) or not isinstance(ready.get("pipe"), str):
            self._release()
            return BrokerStatus(False, "bad_start_line")
        # The broker writes nothing after its start line; drain anyway so a full pipe never blocks it.
        threading.Thread(target=self._drain, args=(self._process.stdout,), daemon=True).start()
        return BrokerStatus(True, "", ready["pipe"], int(self._process.pid))

    def _exchange(self, frame: bytes) -> bytes | None:
        """Send the bootstrap and read the start line: b"" when the broker closed stdout first, None
        after the timeout or a stop. Both run on a worker, so a broker that never reads its stdin
        cannot block the caller either."""
        process = self._process
        result: list[bytes] = []

        def run() -> None:
            # A broker that refuses the frame writes its refusal and exits, so the write may fail;
            # the start line is read anyway, so the refusal's code is what gets reported.
            with contextlib.suppress(OSError, ValueError):
                process.stdin.write(frame)
                process.stdin.flush()
            try:
                result.append(process.stdout.readline())
            except (OSError, ValueError):
                result.append(b"")

        self._worker = threading.Thread(target=run, name="windows-broker-start", daemon=True)
        self._worker.start()
        deadline = time.monotonic() + self.timeout
        while self._worker.is_alive() and not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._worker.join(min(remaining, 0.1))
        return result[0] if result else None

    @staticmethod
    def _drain(stream: Any) -> None:
        with contextlib.suppress(OSError, ValueError):
            while stream.readline():
                pass

    def _release(self) -> None:
        """Closing the job ends the broker, which is then reaped. Its stdout is never closed here:
        a reader blocked on it holds the stream's lock until the pipe ends, so closing it could hang
        the API's exit. stdin is closed only once the start worker has finished writing to it."""
        process, job = self._process, self._job
        self._process, self._job = None, None
        if job is not None:
            job.close()
        if process is None:
            return
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        worker, self._worker = self._worker, None
        if process.stdin is not None and (worker is None or not worker.is_alive()):
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("Windows broker pid %s did not exit after its job closed", process.pid)


def start_for_api(
    project_root: Path, vool_home: Path, *, environ: Mapping[str, str] | None = None, background: bool = False
) -> BrokerHost:
    """Start the broker for this API process. Never raises; the host's status says what happened.

    With ``background`` the start runs on a daemon thread and this returns at once, so the API
    binds its port without waiting on the broker; stop() is still safe at any moment."""
    env = os.environ if environ is None else environ
    journal_dir = vool_home / "broker" / "journal"
    executable = broker_executable(project_root) if _is_windows() else None
    if executable is None:
        host = BrokerHost(Path("VoolBroker.exe"), [], journal_dir)
        host.status = BrokerStatus(False, "not_windows" if not _is_windows() else "not_found")
        logger.info("Windows broker not started (%s)", host.status.reason)
        return host
    # Every listed folder goes to the broker as written, so the broker applies its own checks
    # and a refusal shows in the log, rather than being filtered out here first. The list uses
    # the Windows path separator (os.pathsep there), as core/windows_write_gate.py reads it.
    flagged = [part.strip() for part in env.get(_FLAGGED_ENV, "").split(";") if part.strip()]
    host = BrokerHost(executable, flagged, journal_dir)
    if background:
        threading.Thread(target=host.start, name="windows-broker-start", daemon=True).start()
    else:
        host.start()
    return host


def _current_process_identity() -> tuple[int, int]:
    """This process's id and creation time (FILETIME), which the broker watches."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    get_process_times.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    if not get_process_times(kernel32.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    creation = times[0]
    return os.getpid(), creation.dwHighDateTime << 32 | creation.dwLowDateTime


class _WindowsJob:
    """A Job object with KILL_ON_JOB_CLOSE, held only by this process (its handle is not inherited)."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: int | None = handle
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid: int) -> None:
        kernel32, ctypes = self._kernel32, self._ctypes
        process = kernel32.OpenProcess(self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
