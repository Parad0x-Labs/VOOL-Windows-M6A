"""ComfyUI detection on Windows: no pgrep, and "unknown" is distinct from "not running".

The Windows platform is simulated by patching the module's platform probe, so these run the same
on Linux CI and on a Windows host.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from core import system_resources as sr


class _Proc:
    def __init__(self, pid, cmdline):
        self.info = {"pid": pid, "cmdline": cmdline}


def _fake_psutil(procs=None, *, error=None):
    mod = types.ModuleType("psutil")

    def process_iter(attrs=None):
        if error is not None:
            raise error
        return iter(procs or [])

    mod.process_iter = process_iter
    return mod


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sr.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sr, "_COMFYUI_PORT", "8188")

    def _no_pgrep(cmd, timeout=2.0):
        raise AssertionError(f"Windows must not shell out to {cmd[0]}")

    monkeypatch.setattr(sr, "_run", _no_pgrep)

    def _no_kill(*_a, **_k):
        raise AssertionError("os.kill terminates processes on Windows; never use it to probe")

    monkeypatch.setattr(os, "kill", _no_kill)
    monkeypatch.setattr("core.process_liveness.pid_alive", lambda pid: True)


def _install(monkeypatch, mod):
    monkeypatch.setitem(sys.modules, "psutil", mod)


def test_windows_finds_running_comfyui_without_pgrep(windows, monkeypatch):
    cmd = [r"C:\ComfyUI\python\python.exe", r"C:\ComfyUI\main.py", "--port", "8188"]
    _install(monkeypatch, _fake_psutil([_Proc(10, ["notepad.exe"]), _Proc(4321, cmd)]))
    assert sr._comfyui_state() is True
    assert sr._comfyui_running() is True


def test_windows_equals_form_of_port_matches(windows, monkeypatch):
    cmd = ["python.exe", r"C:\ComfyUI\main.py", "--port=8188"]
    _install(monkeypatch, _fake_psutil([_Proc(4321, cmd)]))
    assert sr._comfyui_state() is True


def test_windows_other_port_is_not_running(windows, monkeypatch):
    cmd = ["python.exe", r"C:\ComfyUI\main.py", "--port", "9999"]
    _install(monkeypatch, _fake_psutil([_Proc(4321, cmd), _Proc(5, None)]))
    assert sr._comfyui_state() is False
    assert sr._comfyui_running() is False


def test_windows_dead_pid_is_not_running(windows, monkeypatch):
    monkeypatch.setattr("core.process_liveness.pid_alive", lambda pid: False)
    cmd = ["python.exe", r"C:\ComfyUI\main.py", "--port", "8188"]
    _install(monkeypatch, _fake_psutil([_Proc(4321, cmd)]))
    assert sr._comfyui_state() is False


def test_windows_enumeration_failure_is_unknown_not_false(windows, monkeypatch):
    _install(monkeypatch, _fake_psutil(error=RuntimeError("enumeration failed")))
    assert sr._comfyui_state() is None
    assert sr._comfyui_running() is False


def test_windows_missing_psutil_is_unknown(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # makes `import psutil` raise ImportError
    assert sr._comfyui_state() is None


def test_snapshot_exposes_unknown_comfyui(windows, monkeypatch):
    _install(monkeypatch, _fake_psutil(error=RuntimeError("enumeration failed")))
    monkeypatch.setattr(sr, "_windows_ram_total_and_available", lambda: (16.0 * sr._GIB, 8.0 * sr._GIB))
    monkeypatch.setattr(sr, "_ollama_loaded_bytes", lambda **_k: 0.0)
    snap = sr._build_snapshot()
    assert snap.comfyui_running is False
    assert snap.comfyui_known is False
    assert snap.to_dict()["comfyui_known"] is False


def test_snapshot_known_when_process_found(windows, monkeypatch):
    cmd = ["python.exe", r"C:\ComfyUI\main.py", "--port", "8188"]
    _install(monkeypatch, _fake_psutil([_Proc(4321, cmd)]))
    monkeypatch.setattr(sr, "_windows_ram_total_and_available", lambda: (16.0 * sr._GIB, 8.0 * sr._GIB))
    monkeypatch.setattr(sr, "_ollama_loaded_bytes", lambda **_k: 0.0)
    snap = sr._build_snapshot()
    assert snap.comfyui_running is True
    assert snap.comfyui_known is True


def test_posix_still_uses_pgrep(monkeypatch):
    monkeypatch.setattr(sr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sr, "_COMFYUI_PORT", "8188")
    seen = []

    def _run(cmd, timeout=2.0):
        seen.append(cmd)
        return "123\n"

    monkeypatch.setattr(sr, "_run", _run)
    assert sr._comfyui_state() is True
    assert seen == [["pgrep", "-f", "main.py --port 8188"]]
