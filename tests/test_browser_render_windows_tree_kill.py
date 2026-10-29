"""Windows contract for the browser render timeout teardown.

On Windows there is no ``os.killpg``/``os.getpgid`` (and no ``signal.SIGKILL``): a
timed-out Chrome's whole tree is taken down with ``taskkill /T /F`` through the
recorded child, or the GPU/network/renderer helpers keep holding the port and the
profile lock. At the old head the AttributeError escaped the except tuple, so the
timeout path itself crashed.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from tools.browser import browser_render as br

pytestmark = pytest.mark.skipif(os.name != "nt", reason="pins the Windows timeout teardown contract")

# Windows' own taskkill by absolute path: a bare name is a PATH lookup a planted binary could win.
_SYSTEM_TASKKILL = os.path.join(os.environ.get("SystemRoot", ""), "System32", "taskkill.exe")


class _HungChrome:
    def __init__(self) -> None:
        self.pid = 4242
        self.killed = False

    def communicate(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="chrome", timeout=timeout or 1)
        return b"", b""

    def kill(self) -> None:
        self.killed = True


def test_a_timeout_kills_the_recorded_child_tree_not_just_the_child(monkeypatch) -> None:
    chrome = _HungChrome()
    ran: list[list[str]] = []
    monkeypatch.setattr(br.subprocess, "Popen", lambda *a, **k: chrome)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        br._run_chrome(["C:\\fake\\chrome.exe"], timeout_s=1.0)
    assert ran == [[_SYSTEM_TASKKILL, "/PID", "4242", "/T", "/F"]], ran
    assert chrome.killed is False  # the tree kill replaced the bare child kill


def test_a_failed_tree_kill_still_kills_the_recorded_child(monkeypatch) -> None:
    chrome = _HungChrome()
    monkeypatch.setattr(br.subprocess, "Popen", lambda *a, **k: chrome)
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 128))
    with pytest.raises(subprocess.TimeoutExpired):
        br._run_chrome(["C:\\fake\\chrome.exe"], timeout_s=1.0)
    assert chrome.killed is True


def test_a_hung_tree_kill_still_kills_the_recorded_child(monkeypatch) -> None:
    chrome = _HungChrome()

    def hung(argv, **k):
        raise subprocess.TimeoutExpired(argv, k.get("timeout") or 5)

    monkeypatch.setattr(br.subprocess, "Popen", lambda *a, **k: chrome)
    monkeypatch.setattr(subprocess, "run", hung)
    with pytest.raises(subprocess.TimeoutExpired):
        br._run_chrome(["C:\\fake\\chrome.exe"], timeout_s=1.0)
    assert chrome.killed is True
