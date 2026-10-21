"""Starting and stopping the broker with the VOOL API (phase B): nothing is routed through it."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.windows_broker import process as broker_process
from core.windows_broker.process import BrokerHost, broker_executable, start_for_api


class _FakeJob:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.assigned: list[int] = []
        self.closed = 0

    def assign(self, pid: int) -> None:
        self.events.append("assign")
        self.assigned.append(pid)

    def close(self) -> None:
        self.events.append("close_job")
        self.closed += 1


class _FakeStdin:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.events.append("bootstrap")
        self.data += data
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, lines: list[bytes], block: threading.Event | None = None) -> None:
        self.lines = list(lines)
        self.block = block

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.block is not None:
            self.block.wait(30)
        return b""

    def close(self) -> None:
        if self.block is not None:
            self.block.set()


class _FakeProcess:
    def __init__(self, events: list[str], lines: list[bytes], *, exits: int | None = None, block: bool = False) -> None:
        self.events = events
        self.pid = 4242
        self.stdin = _FakeStdin(events)
        self.unblock = threading.Event() if block else None
        self.stdout = _FakeStdout(lines, self.unblock)
        self.stderr = None
        self.returncode = exits
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.events.append("kill")
        self.killed = True
        self.returncode = 1
        if self.unblock is not None:
            self.unblock.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _host(tmp_path: Path, lines: list[bytes], **kwargs: Any) -> tuple[BrokerHost, list[str], dict[str, Any]]:
    events: list[str] = []
    made: dict[str, Any] = {}

    def popen(argv: list[str], **options: Any) -> _FakeProcess:
        made["argv"] = argv
        made["options"] = options
        made["process"] = _FakeProcess(events, lines, exits=kwargs.pop("exits", None), block=kwargs.pop("block", False))
        return made["process"]

    def job_factory() -> _FakeJob:
        made["job"] = _FakeJob(events)
        return made["job"]

    exe = tmp_path / "VoolBroker.exe"
    exe.write_bytes(b"")
    host = BrokerHost(
        exe,
        [r"C:\work\a", r"C:\work\b"],
        tmp_path / "journal",
        popen=popen,
        job_factory=job_factory,
        api_identity=lambda: (1234, 5678),
        timeout=kwargs.pop("timeout", 5.0),
    )
    return host, events, made


def _ready(pipe: str = r"\\.\pipe\vool-broker-test") -> bytes:
    return json.dumps({"pipe": pipe, "broker_pid": 4242, "flagged": [], "journal": {"available": True}}).encode() + b"\n"


def test_the_installed_layout_finds_the_broker_beside_the_app(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "broker").mkdir()
    exe = tmp_path / "broker" / "VoolBroker.exe"
    exe.write_bytes(b"")
    assert broker_executable(tmp_path / "app") == exe


def test_a_checkout_finds_the_published_broker(tmp_path: Path) -> None:
    published = tmp_path / "windows" / "broker" / "publish"
    published.mkdir(parents=True)
    (published / "VoolBroker.exe").write_bytes(b"")
    assert broker_executable(tmp_path) == published / "VoolBroker.exe"


def test_no_broker_on_disk_is_none(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert broker_executable(tmp_path / "app") is None


def test_start_puts_the_broker_in_its_job_before_sending_the_bootstrap(tmp_path: Path) -> None:
    host, events, made = _host(tmp_path, [_ready()])
    status = host.start()
    try:
        assert status.running and status.reason == ""
        assert status.pipe == r"\\.\pipe\vool-broker-test" and status.pid == 4242
        # The broker waits on stdin, so the job holds it before it can do anything.
        assert events[:2] == ["assign", "bootstrap"]
        assert made["job"].assigned == [4242]
        data = made["process"].stdin.data
        (length,) = struct.unpack("<I", data[:4])
        bootstrap = json.loads(data[4 : 4 + length])
        assert len(data) == 4 + length
        assert bootstrap["flagged"] == [r"C:\work\a", r"C:\work\b"]
        assert bootstrap["journal_dir"] == str(tmp_path / "journal")
        assert bootstrap["api_pid"] == 1234 and bootstrap["api_creation_time"] == 5678
        assert len(bytes.fromhex(bootstrap["secret"])) == 32
        assert host.secret == bytes.fromhex(bootstrap["secret"])
        # The broker exits when its stdin closes, so the host keeps it open while it runs.
        assert not made["process"].stdin.closed
        assert made["argv"] == [str(tmp_path / "VoolBroker.exe")]
        assert made["options"]["stdin"] == subprocess.PIPE and made["options"]["stdout"] == subprocess.PIPE
        assert made["options"]["stderr"] == subprocess.DEVNULL
    finally:
        host.stop()


def test_stop_closes_the_job_and_stdin_once(tmp_path: Path) -> None:
    host, events, made = _host(tmp_path, [_ready()])
    assert host.start().running
    host.stop()
    host.stop()
    assert made["job"].closed == 1
    assert made["process"].stdin.closed
    assert not host.status.running and host.status.reason == "stopped"


def test_a_refused_start_reports_its_code_and_leaves_nothing_running(tmp_path: Path) -> None:
    refusal = json.dumps({"error": {"entry": r"C:\work\a", "code": "reparse_point", "message": "a junction"}}).encode() + b"\n"
    host, _events, made = _host(tmp_path, [refusal], exits=3)
    status = host.start()
    assert not status.running
    assert status.reason == "refused:reparse_point"
    assert made["job"].closed == 1
    assert made["process"].stdin.closed


def test_a_broker_that_exits_without_a_start_line_is_reported(tmp_path: Path) -> None:
    host, _events, made = _host(tmp_path, [], exits=5)
    status = host.start()
    assert not status.running and status.reason == "exited:5"
    assert made["job"].closed == 1


def test_a_silent_broker_is_killed_after_the_timeout(tmp_path: Path) -> None:
    host, events, made = _host(tmp_path, [], block=True, timeout=0.2)
    started = time.monotonic()
    status = host.start()
    assert time.monotonic() - started < 5
    assert not status.running and status.reason == "start_timeout"
    assert made["process"].killed and made["job"].closed == 1


def test_an_unreadable_start_line_is_refused(tmp_path: Path) -> None:
    host, _events, made = _host(tmp_path, [b"not json\n"])
    status = host.start()
    assert not status.running and status.reason == "bad_start_line"
    assert made["process"].killed and made["job"].closed == 1


def test_a_failed_launch_is_reported_not_raised(tmp_path: Path) -> None:
    def popen(argv: list[str], **options: Any) -> Any:
        raise OSError("cannot run")

    jobs: list[_FakeJob] = []

    def job_factory() -> _FakeJob:
        jobs.append(_FakeJob([]))
        return jobs[-1]

    exe = tmp_path / "VoolBroker.exe"
    exe.write_bytes(b"")
    host = BrokerHost(exe, [], tmp_path / "journal", popen=popen, job_factory=job_factory, api_identity=lambda: (1, 2))
    status = host.start()
    assert not status.running and status.reason == "start_failed"
    assert all(job.closed == 1 for job in jobs)


def test_start_for_api_does_nothing_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_process, "_is_windows", lambda: False)
    host = start_for_api(tmp_path, tmp_path / "home", environ={})
    assert not host.status.running and host.status.reason == "not_windows"
    host.stop()


def test_start_for_api_without_a_broker_reports_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker_process, "_is_windows", lambda: True)
    host = start_for_api(tmp_path, tmp_path / "home", environ={})
    assert not host.status.running and host.status.reason == "not_found"
    host.stop()


def test_start_for_api_passes_the_flagged_entries_and_the_journal_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, executable: Path, flagged: list[str], journal_dir: Path) -> None:
            seen.update(executable=executable, flagged=flagged, journal_dir=journal_dir)
            self.status = broker_process.BrokerStatus(False, "recorded")

        def start(self) -> broker_process.BrokerStatus:
            return self.status

    published = tmp_path / "windows" / "broker" / "publish"
    published.mkdir(parents=True)
    (published / "VoolBroker.exe").write_bytes(b"")
    monkeypatch.setattr(broker_process, "_is_windows", lambda: True)
    monkeypatch.setattr(broker_process, "BrokerHost", _Recorder)
    environ = {"VOOL_WINDOWS_DISPOSABLE_WORKSPACES": ";".join([r" C:\work\a ", "", r"C:\work\b"])}
    start_for_api(tmp_path, tmp_path / "home", environ=environ)
    # Every listed entry goes to the broker as written (trimmed), so the broker applies its own
    # checks and a refusal is visible in the log, instead of the entry being dropped first.
    assert seen["flagged"] == [r"C:\work\a", r"C:\work\b"]
    assert seen["journal_dir"] == tmp_path / "home" / "broker" / "journal"
    assert seen["executable"] == published / "VoolBroker.exe"


# --- the real broker on Windows -------------------------------------------------------------


def _built_broker() -> Path:
    if os.name != "nt":
        pytest.skip("requires Windows")
    root = Path(__file__).resolve().parents[2] / "windows" / "broker"
    for candidate in (
        root / "publish" / "VoolBroker.exe",
        root / "src" / "VoolBroker" / "bin" / "Debug" / "net10.0-windows" / "VoolBroker.exe",
    ):
        if candidate.is_file():
            return candidate
    pytest.skip("built broker executable not found")


def _gone(pid: int, seconds: float = 10.0) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True
        try:
            if kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 100) == 0:
                return True
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return False


def test_the_real_broker_starts_answers_and_stops(tmp_path: Path) -> None:
    executable = _built_broker()
    from core.windows_broker.client import BrokerClient

    host = BrokerHost(executable, [], tmp_path / "journal")
    status = host.start()
    try:
        assert status.running, status.reason
        client = BrokerClient(status.pipe, host.secret, status.pid)
        client.connect()
        try:
            assert client.request("ping")["ok"] is True
        finally:
            client.close()
    finally:
        host.stop()
    assert _gone(status.pid)


def test_closing_the_job_alone_ends_the_broker(tmp_path: Path) -> None:
    executable = _built_broker()
    host = BrokerHost(executable, [], tmp_path / "journal")
    status = host.start()
    assert status.running, status.reason
    try:
        # Only the job goes: stdin stays open, so the broker cannot have left on its own.
        host._close_job_for_tests()
        assert _gone(status.pid)
    finally:
        host.stop()


def test_a_killed_api_takes_its_broker_with_it(tmp_path: Path) -> None:
    executable = _built_broker()
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from core.windows_broker.process import BrokerHost\n"
        "host = BrokerHost(Path(sys.argv[1]), [], Path(sys.argv[2]))\n"
        "status = host.start()\n"
        "print(status.pid if status.running else 'failed ' + status.reason, flush=True)\n"
        "time.sleep(120)\n"
    )
    root = Path(__file__).resolve().parents[2]
    api = subprocess.Popen(
        [sys.executable, "-c", script, str(executable), str(tmp_path / "journal")],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert api.stdout is not None
        line = api.stdout.readline().decode().strip()
        assert line.isdigit(), line
        broker_pid = int(line)
        api.kill()
        api.wait(timeout=10)
        assert _gone(broker_pid)
    finally:
        if api.poll() is None:
            api.kill()
            api.wait(timeout=10)
        if api.stdout is not None:
            api.stdout.close()
