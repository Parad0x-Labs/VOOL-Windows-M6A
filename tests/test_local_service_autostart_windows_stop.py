"""Windows contract for stopping a locally-autostarted service.

On Windows there is no ``os.getpgid``/``os.killpg``: the only addressable target is
the recorded child PID itself. ``stop_started_service`` must keep its contract there
— only the recorded child, never a lookup by name or port, and never an exception
(its docstring promises "Never raises").
"""
from __future__ import annotations

import os
import signal

import pytest

import core.local_service_autostart as autostart

pytestmark = pytest.mark.skipif(os.name != "nt", reason="pins the Windows stop contract")


class _Running:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


def test_stop_signals_the_recorded_child_pid_and_never_raises(monkeypatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(autostart, "_SPAWNED", {"comfy": _Running(4242)})
    monkeypatch.setattr(autostart.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert autostart.stop_started_service("comfy") is True
    assert killed == [(4242, signal.SIGTERM)], killed


def test_stop_never_touches_a_service_this_run_did_not_start(monkeypatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(autostart, "_SPAWNED", {})
    monkeypatch.setattr(autostart.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert autostart.stop_started_service("comfy") is False
    assert killed == []
