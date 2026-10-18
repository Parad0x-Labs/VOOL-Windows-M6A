"""Asking whether a process is alive never kills it.

On Windows `os.kill(pid, 0)` is TerminateProcess with exit code 0, not a probe. The installed
window host's boot-time pidfile sweep "checked" the live API that way: the API exited with
returncode 0 two seconds after the window launched (install smoke run 39094759487). The browser
lane's registry status did the same to a session's engine, or to any process that had since been
given its pid. These run real processes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

from core.process_liveness import pid_alive
from core.vool_browser import sessions
from installer.bundle import vool_window


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _assert_survives(proc: subprocess.Popen) -> None:
    time.sleep(0.5)
    assert proc.poll() is None, f"the liveness probe killed the process (returncode={proc.returncode})"


def test_a_live_process_reads_alive_and_survives_the_probe() -> None:
    proc = _sleeper()
    try:
        assert pid_alive(proc.pid) is True
        _assert_survives(proc)
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_an_exited_process_reads_dead() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert pid_alive(proc.pid) is False


def test_no_pid_is_alive_below_one() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-4) is False


def test_the_window_hosts_pidfile_sweep_probe_leaves_the_api_running() -> None:
    proc = _sleeper()
    try:
        assert vool_window._pid_alive(proc.pid) is True
        _assert_survives(proc)
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_a_browser_session_status_after_restart_leaves_its_engine_running(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_BROWSER_SCRATCH_ROOT", str(tmp_path))
    proc = _sleeper()
    try:
        (tmp_path / "registry.json").write_text(
            json.dumps({"shop": {"session": "shop", "state": "open", "pid": proc.pid}}), encoding="utf-8"
        )
        record = sessions.registry_status("shop")
        assert record is not None and record["state"] == "recoverable"
        _assert_survives(proc)
    finally:
        proc.kill()
        proc.wait(timeout=15)
