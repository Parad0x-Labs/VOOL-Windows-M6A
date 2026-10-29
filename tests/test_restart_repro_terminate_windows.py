from __future__ import annotations

import signal
import subprocess

import pytest

from ops.restart_repro import harness


class _FakeProcess:
    """Daemon child that ignores the polite stop and only dies on kill()."""

    pid = 424242

    def __init__(self) -> None:
        self.killed = False
        self.wait_timeouts: list[float] = []

    def poll(self):
        return 0 if self.killed else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if not self.killed:
            raise subprocess.TimeoutExpired("daemon", timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def _daemon(tmp_path, process):
    daemon = harness.Daemon(
        app_dir=tmp_path, home=tmp_path / "h", workspace=tmp_path / "w", stub_port=1, run_dir=tmp_path
    )
    daemon.process = process
    return daemon


@pytest.fixture
def calls(monkeypatch):
    recorded = {"os_kill": [], "run": []}

    def fake_os_kill(pid, sig):
        recorded["os_kill"].append((pid, sig))
        raise AssertionError("os.kill must not be used to stop the daemon on Windows")

    def fake_run(args, **kwargs):
        recorded["run"].append(list(args))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(harness.os, "kill", fake_os_kill)
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    return recorded


def test_windows_stop_uses_taskkill_tree_then_kill_without_sigkill(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(harness, "_IS_WINDOWS", True, raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)  # Windows has no SIGKILL
    process = _FakeProcess()
    daemon = _daemon(tmp_path, process)

    daemon.terminate()

    assert calls["os_kill"] == []
    assert calls["run"] == [["taskkill", "/PID", "424242", "/T", "/F"]]
    assert process.killed is True
    assert daemon.process is None


def test_windows_stop_survives_taskkill_failure(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(harness, "_IS_WINDOWS", True, raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)

    def broken_run(args, **kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(harness.subprocess, "run", broken_run)
    process = _FakeProcess()
    daemon = _daemon(tmp_path, process)

    daemon.terminate()

    assert calls["os_kill"] == []
    assert process.killed is True
    assert daemon.process is None


def test_posix_stop_keeps_sigterm_then_sigkill(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_IS_WINDOWS", False, raising=False)
    sent = []
    monkeypatch.setattr(harness.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: pytest.fail("taskkill on POSIX"))
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    process = _FakeProcess()
    daemon = _daemon(tmp_path, process)
    original_wait = process.wait

    def wait(timeout=None):
        if timeout == 5:
            process.killed = True
        return original_wait(timeout)

    process.wait = wait

    daemon.terminate()

    assert sent == [(424242, signal.SIGTERM), (424242, 9)]
    assert daemon.process is None
