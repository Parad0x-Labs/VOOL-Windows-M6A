"""Windows contract for the media worker's crash-kill.

On Windows there are no POSIX process groups (``os.killpg``/``os.getpgid`` do not
exist and ``signal.SIGKILL`` is absent): the worker's whole process TREE is taken
down with ``taskkill /T /F`` through the recorded child, so an in-flight ffmpeg
child can never keep writing after the worker is killed. At the old head the
AttributeError was swallowed by ``_kill``'s broad except, the kill silently never
happened, and the worker leaked.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from core.media_worker_client import NebulaWorkerClient

pytestmark = pytest.mark.skipif(os.name != "nt", reason="pins the Windows crash-kill contract")

# Windows' own taskkill by absolute path: a bare name is a PATH lookup a planted binary could win.
_SYSTEM_TASKKILL = os.path.join(os.environ.get("SystemRoot", ""), "System32", "taskkill.exe")


class _RunningWorker:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.waited = False
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_kill_takes_down_the_recorded_child_tree(monkeypatch) -> None:
    client = NebulaWorkerClient()
    worker = _RunningWorker(4242)
    client._proc = worker
    ran: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    client._kill()
    assert ran == [[_SYSTEM_TASKKILL, "/PID", "4242", "/T", "/F"]], ran
    assert worker.waited is True
    assert client._proc is None


def test_without_systemroot_no_taskkill_is_looked_up_and_only_the_child_is_killed(monkeypatch) -> None:
    client = NebulaWorkerClient()
    worker = _RunningWorker(4242)
    client._proc = worker
    ran: list[list[str]] = []
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    client._kill()
    assert ran == [], ran  # fail closed: never a bare "taskkill" found on PATH
    assert worker.killed is True
    assert client._proc is None


@pytest.mark.parametrize("outcome", ["exit 128", "hung"])
def test_a_failed_or_hung_tree_kill_still_kills_the_recorded_child(monkeypatch, outcome) -> None:
    client = NebulaWorkerClient()
    worker = _RunningWorker(4242)
    client._proc = worker

    def run(argv, **k):
        if outcome == "hung":
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 5)
        return subprocess.CompletedProcess(argv, 128)

    monkeypatch.setattr(subprocess, "run", run)
    client._kill()
    assert worker.killed is True
    assert worker.waited is True
    assert client._proc is None
