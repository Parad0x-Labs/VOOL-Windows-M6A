"""Wrapper mode on Windows must launch Start_VOOL.bat through cmd, never ``bash Start_VOOL.sh``."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from core.runtime_paths import runtime_home_identity
from installer.bundle import native_runtime_supervisor as nrs
from installer.bundle.native_runtime_supervisor import NativeRuntimeError, NativeRuntimeSupervisor

SHA = "a" * 40
COMSPEC = r"C:\Windows\System32\cmd.exe"


def _module(tmp_path: Path) -> Path:
    module = tmp_path / "installer" / "bundle" / "vool_window.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    return module


def _wrapper_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOOL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VOOL_RUNTIME_MODE", "wrapper")
    monkeypatch.setenv("VOOL_EXPECTED_COMMIT", SHA)
    monkeypatch.setenv("ComSpec", COMSPEC)
    monkeypatch.setattr(sys, "platform", "win32")


def test_windows_wrapper_mode_runs_the_bat_through_cmd_not_bash(tmp_path, monkeypatch) -> None:
    module = _module(tmp_path)
    bat = tmp_path / "Start_VOOL.bat"
    bat.write_text("@echo off\r\n")
    (tmp_path / "Start_VOOL.sh").write_text("#!/bin/sh\n")
    _wrapper_env(monkeypatch, tmp_path)

    supervisor = NativeRuntimeSupervisor.from_environment(module_path=module)

    assert supervisor.command == [COMSPEC, "/d", "/s", "/c", str(bat.resolve())]
    assert "bash" not in supervisor.command


def test_windows_wrapper_mode_without_the_bat_fails_closed_and_spawns_nothing(tmp_path, monkeypatch) -> None:
    module = _module(tmp_path)
    (tmp_path / "Start_VOOL.sh").write_text("#!/bin/sh\n")  # unusable on Windows; must not be a fallback
    _wrapper_env(monkeypatch, tmp_path)

    supervisor = NativeRuntimeSupervisor.from_environment(module_path=module)
    supervisor._health = mock.Mock(return_value=None)
    popen = mock.Mock()
    supervisor._popen = popen

    with pytest.raises(NativeRuntimeError, match="starter is missing"):
        supervisor.ensure_ready()
    popen.assert_not_called()


def test_posix_wrapper_mode_still_runs_the_sh_through_bash(tmp_path, monkeypatch) -> None:
    module = _module(tmp_path)
    _wrapper_env(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")

    supervisor = NativeRuntimeSupervisor.from_environment(module_path=module)

    assert supervisor.command == ["bash", str((tmp_path / "Start_VOOL.sh").resolve())]


# --- Owned Windows wrapper runtime: cmd.exe is the root, the API is its descendant -------------


class _NtOs:
    """The module's ``os`` as seen on Windows, without turning pathlib Windows-only."""

    name = "nt"

    def __getattr__(self, attr):
        return getattr(os, attr)


class _Child:
    def __init__(self, pid: int = 9100) -> None:
        self.pid = pid
        self.returncode = None
        self.events: list[str] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.events.append("wait")
        self.returncode = 0
        return 0

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")


def _health(pid: int, home: Path) -> dict:
    return {
        "ok": True,
        "agent": "Atlas",
        "runtime": {
            "commit": SHA[:12],
            "commit_full": SHA,
            "pid": pid,
            "protocol_version": 1,
            "release_version": "0.7.1-test",
            "workstation_version": "vool-workstation-test-v1",
            "runtime_home_id": runtime_home_identity({"VOOL_HOME": str(home)}),
        },
    }


def _fake_psutil(tree: dict[int, list[int]]):
    mod = types.ModuleType("psutil")

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            out, stack = [], [self.pid]
            while stack:
                for child in tree.get(stack.pop(), []):
                    out.append(Process(child))
                    if recursive:
                        stack.append(child)
            return out

    mod.Process = Process
    return mod


def _windows_owned(tmp_path, monkeypatch, *, root: Path | None = None, tree=None, child=None):
    root = root or tmp_path
    root.mkdir(parents=True, exist_ok=True)
    bat = root / "Start_VOOL.bat"
    bat.write_text("@echo off\r\n")
    child = child or _Child()
    runs: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        runs.append([str(part) for part in cmd])
        child.events.append("taskkill")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(nrs, "os", _NtOs())
    monkeypatch.setattr(nrs.subprocess, "run", fake_run)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(tree if tree is not None else {child.pid: [9200]}))
    supervisor = NativeRuntimeSupervisor(
        root,
        [COMSPEC, "/d", "/s", "/c", str(bat)],
        expected_sha=SHA,
        env={"VOOL_HOME": str(tmp_path / "home")},
        startup_timeout=1,
        poll_interval=0,
        popen=mock.Mock(return_value=child),
        sleep=lambda _: None,
    )
    return supervisor, child, runs, bat


def test_windows_wrapper_spawn_keeps_quotes_for_a_path_cmd_would_split(tmp_path, monkeypatch) -> None:
    supervisor, child, _runs, bat = _windows_owned(tmp_path, monkeypatch, root=tmp_path / "Program Files (x86)" / "R&D")
    supervisor._health = mock.Mock(side_effect=[None, _health(9200, tmp_path / "home")])

    supervisor.ensure_ready()

    spawned = supervisor._popen.call_args.args[0]
    assert spawned == f'"{COMSPEC}" /d /s /c ""{bat}""'


def test_windows_wrapper_starter_path_with_percent_is_refused_before_spawn(tmp_path, monkeypatch) -> None:
    supervisor, _child, _runs, _bat = _windows_owned(tmp_path, monkeypatch, root=tmp_path / "100%done")
    supervisor._health = mock.Mock(return_value=None)

    with pytest.raises(NativeRuntimeError, match="cannot be passed to cmd"):
        supervisor.ensure_ready()
    supervisor._popen.assert_not_called()


def test_windows_wrapper_accepts_the_api_running_under_its_own_cmd(tmp_path, monkeypatch) -> None:
    supervisor, child, runs, _bat = _windows_owned(tmp_path, monkeypatch, tree={9100: [9150], 9150: [9200]})
    supervisor._health = mock.Mock(side_effect=[None, _health(9200, tmp_path / "home")])

    identity = supervisor.ensure_ready()

    assert identity["pid"] == 9200
    assert runs == []
    assert supervisor.owns_runtime is True


def test_windows_wrapper_rejects_an_api_outside_its_own_tree_and_stops_the_tree(tmp_path, monkeypatch) -> None:
    supervisor, child, runs, _bat = _windows_owned(tmp_path, monkeypatch, tree={9100: [9150]})
    supervisor._health = mock.Mock(side_effect=[None, _health(4444, tmp_path / "home")])

    with pytest.raises(NativeRuntimeError, match="identity mismatch"):
        supervisor.ensure_ready()
    assert runs == [["taskkill", "/PID", "9100", "/T", "/F"]]
    assert supervisor.owns_runtime is False


def test_windows_wrapper_shutdown_stops_the_cmd_tree_while_the_root_is_alive(tmp_path, monkeypatch) -> None:
    supervisor, child, runs, _bat = _windows_owned(tmp_path, monkeypatch)
    supervisor._health = mock.Mock(side_effect=[None, _health(9200, tmp_path / "home")])
    supervisor.ensure_ready()

    supervisor.shutdown()

    assert runs == [["taskkill", "/PID", "9100", "/T", "/F"]]
    assert child.events[0] == "taskkill"
    assert supervisor.owns_runtime is False


def test_windows_wrapper_without_psutil_fails_closed(tmp_path, monkeypatch) -> None:
    supervisor, child, runs, _bat = _windows_owned(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "psutil", None)
    supervisor._health = mock.Mock(side_effect=[None, _health(9200, tmp_path / "home")])

    with pytest.raises(NativeRuntimeError, match="identity mismatch"):
        supervisor.ensure_ready()
