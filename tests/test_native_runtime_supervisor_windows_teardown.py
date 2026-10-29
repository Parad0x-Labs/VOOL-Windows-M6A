"""Windows contract for the native runtime supervisor's teardown.

``shutdown`` promises to tear down the process group this host created (its
docstring), because the spawned runtime leaves helpers holding the API port. On
Windows there is no group signalling: the whole tree of the recorded child is taken
down through its PID while the root still runs: once the root has exited, ``taskkill /T``
can no longer walk to the helpers it started. Nothing outside the spawned child's tree is
touched.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from installer.bundle.native_runtime_supervisor import NativeRuntimeSupervisor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="pins the Windows teardown contract")

# Windows' own taskkill by absolute path: a bare name is a PATH lookup a planted binary could win.
_SYSTEM_TASKKILL = os.path.join(os.environ.get("SystemRoot", ""), "System32", "taskkill.exe")


class _LiveChild:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False
        self.signalled: list[int] = []
        self.waited = 0
        self.events: list[str] = []

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def send_signal(self, sig: int) -> None:
        self.signalled.append(sig)

    def wait(self, timeout=None):
        self.waited += 1
        self.events.append("wait")
        return 0


def test_shutdown_takes_down_the_recorded_child_tree(tmp_path, monkeypatch) -> None:
    supervisor = NativeRuntimeSupervisor(tmp_path, ["cmd"], expected_sha="a" * 40)
    child = _LiveChild()
    supervisor.process = child
    ran: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    supervisor.shutdown()
    assert ran == [[_SYSTEM_TASKKILL, "/PID", "4242", "/T", "/F"]], ran
    assert supervisor.owns_runtime is False


def test_the_tree_is_taken_down_while_its_root_still_runs(tmp_path, monkeypatch) -> None:
    supervisor = NativeRuntimeSupervisor(tmp_path, ["cmd"], expected_sha="a" * 40)
    child = _LiveChild()
    supervisor.process = child

    def run(argv, **k):
        child.events.append("taskkill")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    supervisor.shutdown()
    assert child.events.count("taskkill") == 1, child.events
    assert child.events[0] == "taskkill", child.events  # no wait for the root to exit first
    assert child.signalled == []


class _StuckChild(_LiveChild):
    """A root that outlives the tree kill: the first wait times out."""

    def __init__(self) -> None:
        super().__init__()
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        self.waited += 1
        if self.waited == 1:
            raise subprocess.TimeoutExpired("runtime", timeout or 8)
        return 0


@pytest.mark.parametrize("outcome", ["exit 128", "hung"])
def test_a_failed_or_hung_tree_kill_still_kills_the_root(tmp_path, monkeypatch, outcome) -> None:
    supervisor = NativeRuntimeSupervisor(tmp_path, ["cmd"], expected_sha="a" * 40)
    child = _StuckChild()
    supervisor.process = child
    ran: list[list[str]] = []

    def run(argv, **k):
        ran.append(list(argv))
        if outcome == "hung":
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 8)
        return subprocess.CompletedProcess(argv, 128)

    monkeypatch.setattr(subprocess, "run", run)
    supervisor.shutdown()
    assert ran == [[_SYSTEM_TASKKILL, "/PID", "4242", "/T", "/F"]], ran
    assert child.killed is True
    assert supervisor.owns_runtime is False
