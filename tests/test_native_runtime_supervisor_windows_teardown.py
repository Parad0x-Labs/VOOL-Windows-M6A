"""Windows contract for the native runtime supervisor's teardown.

``shutdown`` promises to tear down the process group this host created (its
docstring), because the spawned runtime leaves helpers holding the API port. On
Windows there is no group signalling: the whole tree of the recorded child is taken
down through its PID, after a graceful CTRL_BREAK attempt at the child's own group
(the spawn side created one). Nothing outside the spawned child's tree is touched.
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
