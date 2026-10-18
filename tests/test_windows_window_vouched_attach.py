"""The Windows window attaches to the exact API its bundle supervisor started, which vouches for it.

Attached, the upstream watchdog treats one unanswered health probe as fatal: with no owned child
to vouch for the runtime, it cannot tell a busy runtime from a lost one. Install smoke run
39100383587 attached to a first-run API that answered one probe late, and the window closed
itself 30 s before the smoke looked. The bundle supervisor knows the API's pid. Given it
(VOOL_NATIVE_VOUCHING_PID) the window attaches only to that process; while it lives, slow health
gets the owned child's grace; its exit, or another pid answering, is fatal at once. Without it,
the upstream contract is unchanged (test_app_lifecycle_ownership pins that).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from installer.bundle import native_runtime_supervisor as nrs
from tests.test_app_lifecycle_ownership import _HealthServer

_COMMIT = "a" * 40


def _api_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _vouched(tmp_path: Path, api_pid: int, *, served_pid: int | None = None):
    server = _HealthServer(_COMMIT)
    clock = [1000.0]
    sup = nrs.NativeRuntimeSupervisor(
        tmp_path, ["true"], expected_sha=_COMMIT, health_url=server.url, require_owned=False,
        unhealthy_grace=30.0, vouching_pid=api_pid, monotonic=lambda: clock[0],
    )
    server.identity = {
        "pid": served_pid if served_pid is not None else api_pid,
        "runtime_home_id": sup.runtime_home_id,
        "protocol_version": 1,
        "workstation_version": "vool-workstation-0.7",
        "release_version": "0.7.1",
    }
    return sup, server, clock


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=15)


def test_it_attaches_only_to_the_process_the_supervisor_started(tmp_path: Path) -> None:
    api = _api_process()
    try:
        sup, server, _clock = _vouched(tmp_path, api.pid)
        assert sup.ensure_ready()["pid"] == api.pid and sup.attached_existing
        server.close()
        impostor, server, _clock = _vouched(tmp_path, api.pid, served_pid=api.pid + 1)
        with pytest.raises(nrs.NativeRuntimeError, match="other than the one the supervisor started"):
            impostor.ensure_ready()
        assert impostor.attached_existing is False
        server.close()
    finally:
        _stop(api)


def test_slow_health_is_tolerated_while_the_vouching_process_lives_then_fatal_past_the_grace(tmp_path: Path) -> None:
    api = _api_process()
    sup, server, clock = _vouched(tmp_path, api.pid)
    try:
        sup.ensure_ready()
        server.mode = "slow"
        sup.assert_alive()  # first late answer from a busy first-run API: the window stays
        clock[0] += 20.0
        sup.assert_alive()
        clock[0] += 15.0
        with pytest.raises(nrs.NativeRuntimeError, match="health endpoint is unavailable"):
            sup.assert_alive()
    finally:
        server.close()
        _stop(api)


def test_the_vouching_process_exiting_or_another_pid_answering_is_fatal_at_once(tmp_path: Path) -> None:
    api = _api_process()
    sup, server, _clock = _vouched(tmp_path, api.pid)
    try:
        sup.ensure_ready()
        server.identity["pid"] = api.pid + 1
        with pytest.raises(nrs.NativeRuntimeError, match="identity changed"):
            sup.assert_alive()
        server.identity["pid"] = api.pid
        sup.assert_alive()
        _stop(api)
        with pytest.raises(nrs.NativeRuntimeError, match="supervised runtime process exited"):
            sup.assert_alive()
    finally:
        server.close()
        _stop(api)


def test_the_window_host_reads_the_vouching_pid_from_its_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_NATIVE_VOUCHING_PID", "4242")
    monkeypatch.setenv("VOOL_EXPECTED_COMMIT", _COMMIT)
    assert nrs.NativeRuntimeSupervisor.from_environment(module_path=tmp_path / "b" / "vool_window.py").vouching_pid == 4242
    monkeypatch.delenv("VOOL_NATIVE_VOUCHING_PID")
    assert nrs.NativeRuntimeSupervisor.from_environment(module_path=tmp_path / "b" / "vool_window.py").vouching_pid is None
