"""Windows self-update must tree-kill only this installation's API, never a recycled pid's python.exe."""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from installer import self_update
from tests._windows_system32 import POWERSHELL, TASKKILL, trust_system32


@pytest.fixture
def pidfile_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    (home / "data" / "vool_api.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    # Windows' own tools run from System32 only (#97); give this host a System32 whose tools exist.
    trust_system32(monkeypatch)
    # The pid is some live python.exe -- exactly what a recycled pid can also be.
    monkeypatch.setattr(self_update, "_pid_is_python", lambda pid: True)
    return home


def _fake_run(calls, ownership_answer):
    def run(cmd, *args, **kwargs):
        calls.append([str(part) for part in cmd])
        stdout = ownership_answer if cmd[0] == POWERSHELL else ""
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return run


def test_windows_recycled_python_pid_from_another_install_is_never_tree_killed(pidfile_home, tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls, ""))

    self_update._default_stop_server(pidfile_home, tmp_path)

    assert calls and calls[0][0] == POWERSHELL
    assert not any(cmd[0] == TASKKILL for cmd in calls)


def test_windows_owned_api_pid_is_tree_killed(pidfile_home, tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls, "owned\n"))

    self_update._default_stop_server(pidfile_home, tmp_path)

    assert calls[0][0] == POWERSHELL
    assert calls[-1] == [TASKKILL, "/PID", "4242", "/T", "/F"]
