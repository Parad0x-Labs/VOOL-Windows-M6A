"""Windows integration tests are added with the real broker executable."""

from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
from ctypes import wintypes
from pathlib import Path

import pytest


def _broker_executable() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "windows"
        / "broker"
        / "src"
        / "VoolBroker"
        / "bin"
        / "Debug"
        / "net10.0-windows"
        / "VoolBroker.exe"
    )


def _current_process_creation_time() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    get_process_times.restype = wintypes.BOOL
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not get_process_times(
        kernel32.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return creation.dwHighDateTime << 32 | creation.dwLowDateTime


def _start_broker(tmp_path: Path) -> tuple[subprocess.Popen[bytes], bytes, str]:
    executable = _broker_executable()
    if os.name != "nt":
        pytest.skip("requires Windows named pipes and GetProcessTimes")
    if not executable.is_file():
        pytest.skip(f"built broker executable not found: {executable}")

    secret = os.urandom(32)
    bootstrap = {
        "secret": secret.hex(),
        "api_pid": os.getpid(),
        "api_creation_time": _current_process_creation_time(),
        "flagged": [],
        "journal_dir": str(tmp_path / "journal"),
    }
    process = subprocess.Popen(
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdin is not None and process.stdout is not None
    payload = json.dumps(bootstrap, separators=(",", ":")).encode("utf-8")
    process.stdin.write(struct.pack("<I", len(payload)) + payload)
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        process.kill()
        process.wait()
        pytest.fail(f"broker exited before reporting its pipe (exit {process.returncode}): {stderr}")
    ready = json.loads(line.decode("utf-8"))
    return process, secret, ready["pipe"]


def _stop_broker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows broker and named pipes")
def test_client_requires_a_valid_broker_pipe() -> None:
    from core.windows_broker.client import BrokerClient

    with pytest.raises(ValueError, match="pipe name"):
        BrokerClient("not-a-pipe", b"0" * 32, 1)


def test_client_authenticates_and_pings_real_broker(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("requires Windows named pipes and GetProcessTimes")
    from core.windows_broker.client import BrokerClient

    process, secret, pipe_name = _start_broker(tmp_path)
    try:
        client = BrokerClient(pipe_name, secret, process.pid)
        client.connect()
        try:
            response = client.request("ping")
            assert response["ok"] is True
            assert isinstance(response["id"], str) and response["id"]
        finally:
            client.close()
    finally:
        _stop_broker(process)


def test_client_rejects_wrong_secret_from_real_broker(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("requires Windows named pipes and GetProcessTimes")
    from core.windows_broker.client import BrokerClient

    process, _secret, pipe_name = _start_broker(tmp_path)
    try:
        client = BrokerClient(pipe_name, b"\x00" * 32, process.pid)
        with pytest.raises(ConnectionError, match="authentication failed"):
            client.connect()
        client.close()
    finally:
        _stop_broker(process)


def test_client_refuses_a_pipe_whose_server_is_not_the_broker(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("requires Windows named pipes and GetProcessTimes")
    from core.windows_broker.client import BrokerClient

    process, secret, pipe_name = _start_broker(tmp_path)
    try:
        # The real GetNamedPipeServerProcessId check: the expected pid is this test process.
        client = BrokerClient(pipe_name, secret, os.getpid())
        with pytest.raises(ConnectionError, match="unavailable") as refused:
            client.connect()
        assert "not the broker process" in str(refused.value.__cause__)
        client.close()

        assert process.poll() is None
        right = BrokerClient(pipe_name, secret, process.pid)
        right.connect()
        try:
            assert right.request("ping")["ok"] is True
        finally:
            right.close()
    finally:
        _stop_broker(process)
