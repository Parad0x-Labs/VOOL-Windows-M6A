"""Windows teardown of the served-reality daemon tree.

The platform is driven through the module's ``_IS_WINDOWS`` flag so the test
behaves the same on a Windows host and on Linux CI.
"""

from __future__ import annotations

import subprocess

import pytest

from ops.served_reality import daemon
from ops.served_reality.classify import BenchError
from tests._windows_system32 import TASKKILL, trust_system32


class _FakeProcess:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def _forbid_posix_paths(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    def fake_run(cmd, *args, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fail_kill(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) must not be used on Windows")

    trust_system32(monkeypatch)
    monkeypatch.setattr(daemon, "_IS_WINDOWS", True)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon.os, "kill", fail_kill)


def test_windows_terminate_tree_uses_taskkill_and_waits_for_exit(monkeypatch):
    calls: list[list[str]] = []
    _forbid_posix_paths(monkeypatch, calls)
    state = {"alive": True}

    def fake_alive(pid: int) -> bool:
        return state["alive"] if pid == 4242 else False

    def fake_run(cmd, *args, **kwargs):
        calls.append([str(part) for part in cmd])
        if cmd[0] == TASKKILL:
            state["alive"] = False
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon, "pid_alive", fake_alive)
    process = _FakeProcess()

    daemon.terminate_tree(4242, term_wait_s=2.0, process=process)

    assert calls == [[TASKKILL, "/PID", "4242", "/T", "/F"]]
    assert process.killed is False


def test_windows_terminate_tree_skips_dead_root(monkeypatch):
    calls: list[list[str]] = []
    _forbid_posix_paths(monkeypatch, calls)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    daemon.terminate_tree(4242, term_wait_s=0.1, process=_FakeProcess())

    assert calls == []


def test_windows_terminate_tree_falls_back_to_kill_when_still_alive(monkeypatch):
    calls: list[list[str]] = []
    _forbid_posix_paths(monkeypatch, calls)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    process = _FakeProcess()

    daemon.terminate_tree(4242, term_wait_s=0.2, process=process)

    assert calls == [[TASKKILL, "/PID", "4242", "/T", "/F"]]
    assert process.killed is True


def test_windows_terminate_tree_still_refuses_low_pids(monkeypatch):
    calls: list[list[str]] = []
    _forbid_posix_paths(monkeypatch, calls)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)

    with pytest.raises(BenchError):
        daemon.terminate_tree(1, process=_FakeProcess())
    assert calls == []


def test_handle_terminate_passes_its_process_to_the_tree_teardown(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    class _Proc:
        pid = 4242
        returncode = 1

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

    proc = _Proc()

    def fake_terminate_tree(pid, **kwargs):
        seen["pid"] = pid
        seen["process"] = kwargs.get("process")

    monkeypatch.setattr(daemon, "terminate_tree", fake_terminate_tree)
    handle = daemon.DaemonHandle(
        pid=4242,
        port=1,
        process=proc,  # type: ignore[arg-type]
        home=tmp_path,
        app_dir=tmp_path,
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
    )

    assert handle.terminate() == 1
    assert seen == {"pid": 4242, "process": proc}
