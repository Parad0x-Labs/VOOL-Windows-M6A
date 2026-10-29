"""Wrapper mode on Windows must launch Start_VOOL.bat through cmd, never ``bash Start_VOOL.sh``."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

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

    assert supervisor.command == [COMSPEC, "/c", str(bat.resolve())]
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
