"""Windows owner watch: no reparenting, and cmd.exe can sit between the window host and the daemon.

``os.getppid()`` proves nothing there, so the daemon must watch the owning window pid itself,
pinned by its create time so a recycled pid never keeps a headless daemon alive.
"""

from __future__ import annotations

import os
import sys
import time
import types

import pytest

import apps.vool_api_server as srv

OWNER = 7777


class _Server:
    should_exit = False


def _fake_psutil(table: dict[int, float]):
    mod = types.ModuleType("psutil")

    class NoSuchProcess(Exception):
        pass

    class Process:
        def __init__(self, pid):
            if pid not in table:
                raise NoSuchProcess(pid)
            self.pid = pid

        def create_time(self):
            if self.pid not in table:
                raise NoSuchProcess(self.pid)
            return table[self.pid]

    mod.NoSuchProcess = NoSuchProcess
    mod.Process = Process
    return mod


@pytest.fixture
def windows_owner(monkeypatch):
    assert os.getppid() != OWNER  # the daemon's direct parent is not the owner (cmd.exe between)
    table = {OWNER: 1000.0}
    monkeypatch.setattr(srv, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(table))
    monkeypatch.setenv("VOOL_OWNED_BY_WINDOW_PID", str(OWNER))
    server = _Server()
    yield table, server
    server.should_exit = True


def _wait_exit(server, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.should_exit:
            return True
        time.sleep(0.05)
    return False


def test_windows_daemon_keeps_serving_while_its_owner_lives_behind_cmd(windows_owner):
    _table, server = windows_owner
    srv._start_parent_death_watch(server)
    assert _wait_exit(server, 2.5) is False


def test_windows_daemon_stops_when_its_owner_is_gone(windows_owner):
    table, server = windows_owner
    srv._start_parent_death_watch(server)
    table.pop(OWNER)
    assert _wait_exit(server, 3.0) is True


def test_windows_daemon_stops_when_the_owner_pid_is_recycled(windows_owner):
    table, server = windows_owner
    srv._start_parent_death_watch(server)
    table[OWNER] = 2000.0  # same pid, a different (newer) process
    assert _wait_exit(server, 3.0) is True
