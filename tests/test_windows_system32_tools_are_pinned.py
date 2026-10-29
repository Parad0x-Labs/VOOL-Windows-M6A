"""Windows' own tools run from %SystemRoot%\\System32 only, with no console window.

A bare ``tasklist``, ``sc`` or ``cmd`` resolves through the app folder, the current folder and
PATH, so a planted copy there would run instead. Each Windows branch below runs the copy
core/process_liveness pins, and does nothing when that folder can't be trusted. wmic is gone
from Windows 11 24H2, so the hardware probe reads the same WMI classes through Get-CimInstance.
Off Windows the tests pin the platform seam and record each command instead of running it.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import types

import pytest

from core import hardware_tier
from core.operator import system as operator_system
from installer import self_update
from ops import process_tree
from tests._windows_system32 import CMD, POWERSHELL, SC, TASKKILL, TASKLIST, trust_system32

_NO_WINDOW = 0x08000000


class _Recorder:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        return types.SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def _module_with(module, **overrides):
    """A stand-in for ``module`` with some attributes replaced, so the shared module is untouched."""
    namespace = types.SimpleNamespace(**{k: getattr(module, k) for k in dir(module) if not k.startswith("__")})
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --- core/operator/system.py ---------------------------------------------------------------


def test_inspect_processes_runs_system32_tasklist(monkeypatch) -> None:
    trust_system32(monkeypatch)
    run = _Recorder(stdout='"python.exe","4242","Console","1","10,000 K"\n')

    rows = operator_system.inspect_processes(os_name="nt", subprocess_run=run, csv_module=csv)

    assert run.calls == [[TASKLIST, "/fo", "csv", "/nh"]]
    assert rows == [{"pid": "4242", "name": "python.exe", "cpu_percent": 0.0, "mem_percent": 0.0}]


def test_inspect_services_runs_system32_sc(monkeypatch) -> None:
    trust_system32(monkeypatch)
    run = _Recorder(stdout="SERVICE_NAME: Spooler\nDISPLAY_NAME: Print Spooler\nSTATE : 4 RUNNING\n")

    rows = operator_system.inspect_services(os_name="nt", subprocess_run=run, which_fn=lambda _n: None)

    assert run.calls == [[SC, "query", "type=", "service", "state=", "all"]]
    assert rows == [{"name": "Spooler", "state": "4 RUNNING", "detail": "Print Spooler"}]


def test_operator_inspection_runs_nothing_without_a_trusted_windows_folder(monkeypatch) -> None:
    monkeypatch.delenv("SystemRoot", raising=False)
    run = _Recorder()

    assert operator_system.inspect_processes(os_name="nt", subprocess_run=run, csv_module=csv) == []
    assert operator_system.inspect_services(os_name="nt", subprocess_run=run, which_fn=lambda _n: None) == []
    assert run.calls == []


# --- installer/self_update.py --------------------------------------------------------------


def test_pid_is_python_runs_system32_tasklist_without_a_window(monkeypatch) -> None:
    trust_system32(monkeypatch)
    monkeypatch.setattr(self_update, "_NO_WINDOW", _NO_WINDOW, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    run = _Recorder(stdout='"python.exe","4321","Console","1","10,000 K"\n')
    monkeypatch.setattr(self_update, "subprocess", _module_with(subprocess, run=run), raising=False)

    assert self_update._pid_is_python(4321) is True
    assert run.calls == [[TASKLIST, "/FI", "PID eq 4321", "/FO", "CSV", "/NH"]]
    assert run.kwargs[0].get("creationflags") == _NO_WINDOW


def test_pid_is_python_is_false_without_a_trusted_windows_folder(monkeypatch) -> None:
    """Unconfirmed is not python: the caller then leaves the PID alone."""
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    run = _Recorder(stdout='"python.exe","4321","Console","1","10,000 K"\n')
    monkeypatch.setattr(self_update, "subprocess", _module_with(subprocess, run=run), raising=False)

    assert self_update._pid_is_python(4321) is False
    assert run.calls == []


def test_default_start_server_runs_system32_cmd_on_windows(tmp_path, monkeypatch) -> None:
    trust_system32(monkeypatch)
    (tmp_path / "Start_VOOL.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    launched: list[list[str]] = []
    popen = lambda argv, **_kw: launched.append(list(argv))  # noqa: E731
    monkeypatch.setattr(self_update, "subprocess", _module_with(subprocess, Popen=popen), raising=False)

    self_update._default_start_server(tmp_path)

    assert launched == [[CMD, "/c", str(tmp_path / "Start_VOOL.bat")]]


def test_default_start_server_launches_nothing_without_a_trusted_windows_folder(tmp_path, monkeypatch) -> None:
    """The keep-alive watchdog restarts VOOL instead."""
    monkeypatch.delenv("SystemRoot", raising=False)
    (tmp_path / "Start_VOOL.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    launched: list[list[str]] = []
    popen = lambda argv, **_kw: launched.append(list(argv))  # noqa: E731
    monkeypatch.setattr(self_update, "subprocess", _module_with(subprocess, Popen=popen), raising=False)

    self_update._default_start_server(tmp_path)

    assert launched == []


# --- core/hardware_tier.py -----------------------------------------------------------------


def _windows_hardware(monkeypatch, stdout: str) -> _Recorder:
    trust_system32(monkeypatch)
    monkeypatch.setattr(hardware_tier, "_NO_WINDOW", _NO_WINDOW, raising=False)
    monkeypatch.setattr(hardware_tier, "platform", _module_with(hardware_tier.platform, system=lambda: "Windows"))
    run = _Recorder(stdout=stdout)
    monkeypatch.setattr(hardware_tier, "subprocess", _module_with(hardware_tier.subprocess, run=run))
    return run


def test_directml_devices_come_from_get_ciminstance_not_wmic(monkeypatch) -> None:
    run = _windows_hardware(
        monkeypatch,
        "4293918720,AMD Radeon RX 6600, rev. A\n"
        "134217728,Microsoft Basic Display Adapter\n"
        ",Virtual Display\n",
    )

    devices = hardware_tier._try_directml_devices()

    assert len(run.calls) == 1 and run.calls[0][0] == POWERSHELL
    assert "Get-CimInstance Win32_VideoController" in run.calls[0][-1]
    assert not any("wmic" in part.lower() for part in run.calls[0])
    assert run.kwargs[0].get("creationflags") == _NO_WINDOW
    assert [(d.name, d.backend, d.source) for d in devices] == [("AMD Radeon RX 6600, rev. A", "directml", "cim")]
    assert devices[0].vram_gb == pytest.approx(4293918720 / 1024.0**3)


def test_ram_fallback_comes_from_get_ciminstance_not_wmic(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # the import fails, as on a bare install
    run = _windows_hardware(monkeypatch, "17179869184\r\n")

    assert hardware_tier._detect_ram_gb() == pytest.approx(16.0)
    assert len(run.calls) == 1 and run.calls[0][0] == POWERSHELL
    assert "Get-CimInstance Win32_ComputerSystem" in run.calls[0][-1]
    assert run.kwargs[0].get("creationflags") == _NO_WINDOW
    # A cold PowerShell start can take several seconds; a short timeout would drop the reading.
    assert run.kwargs[0].get("timeout", 0) >= 12


def test_hardware_probe_runs_nothing_without_a_trusted_windows_folder(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    run = _windows_hardware(monkeypatch, "17179869184\n")
    monkeypatch.delenv("SystemRoot", raising=False)

    assert hardware_tier._try_directml_devices() == ()
    assert hardware_tier._detect_ram_gb() == 4.0
    assert run.calls == []


# --- ops/process_tree.py -------------------------------------------------------------------


class _Child:
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_tree_taskkill_opens_no_console_window(monkeypatch) -> None:
    trust_system32(monkeypatch)
    monkeypatch.setattr(process_tree, "_NO_WINDOW", _NO_WINDOW, raising=False)
    monkeypatch.setattr(process_tree, "os", _module_with(os, name="nt"))
    run = _Recorder()
    monkeypatch.setattr(process_tree, "subprocess", _module_with(process_tree.subprocess, run=run))

    process_tree.stop_process_tree(_Child(), grace_seconds=0.01)

    assert run.calls == [[TASKKILL, "/PID", "4242", "/T", "/F"]]
    assert run.kwargs[0].get("creationflags") == _NO_WINDOW
