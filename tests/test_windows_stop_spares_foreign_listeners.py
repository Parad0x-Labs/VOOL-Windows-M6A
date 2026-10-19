"""The Windows hard stop (Stop VOOL button, in-chat /stopall) kills only VOOL's own listeners.

It used to tree-kill whatever process listened on 18789 or 11435. OpenClaw is retired from VOOL,
so 18789 is only ever a user's own OpenClaw gateway, and 11435 can be any local server; the stop
killed both, with their child processes. These run real listeners on free ports and the real
PowerShell lookup the stop uses.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from installer.vool_stop import kill_listener

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the Windows hard stop drives PowerShell and taskkill")

_SERVE = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
    "time.sleep(120)\n"
)
# A venv's python.exe on Windows is a launcher that runs the base interpreter as a child, so the
# listener would not be the Popen pid. The base interpreter is the listener itself.
_PYTHON = getattr(sys, "_base_executable", "") or sys.executable


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listen(script: Path, port: int) -> subprocess.Popen:
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_SERVE, encoding="utf-8")
    proc = subprocess.Popen([_PYTHON, str(script), str(port)])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as client:
            if client.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    proc.kill()
    raise AssertionError(f"the test listener never bound 127.0.0.1:{port}")


def _wait_exit(proc: subprocess.Popen) -> int | None:
    try:
        return proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        return None


def test_the_stop_leaves_a_foreign_listener_running(tmp_path) -> None:
    port = _free_port()
    foreign = _listen(tmp_path / "someone_elses_server.py", port)
    try:
        assert kill_listener(subprocess.run, port, tmp_path / "app") is None
        time.sleep(1)
        assert foreign.poll() is None, "the hard stop killed a process VOOL does not own"
    finally:
        foreign.kill()
        foreign.wait(timeout=15)


def test_the_stop_still_kills_vools_own_api_listener(tmp_path) -> None:
    port = _free_port()
    vool_api = _listen(tmp_path / "app" / "apps" / "vool_api_server.py", port)
    try:
        assert kill_listener(subprocess.run, port, tmp_path / "app") == vool_api.pid
        assert _wait_exit(vool_api) is not None, "the VOOL API listener survived the hard stop"
    finally:
        if vool_api.poll() is None:
            vool_api.kill()
            vool_api.wait(timeout=15)
