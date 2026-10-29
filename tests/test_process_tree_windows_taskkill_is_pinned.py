"""ops/process_tree stops a verification child's tree with System32's taskkill on Windows.

A bare ``taskkill`` resolves through the app folder, the current folder and PATH, so a planted
taskkill.exe would run instead. The Windows branch runs the copy core/process_liveness pins; off
Windows the test hands the module a Windows ``os`` and records the command instead of running it.
"""
from __future__ import annotations

import os
import types

from ops import process_tree
from tests._windows_system32 import TASKKILL, trust_system32


class _Child:
    pid = 4242

    def poll(self):
        return None

    def send_signal(self, _sig):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_windows_tree_stop_runs_system32_taskkill(monkeypatch) -> None:
    trust_system32(monkeypatch)
    windows_os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    windows_os.name = "nt"
    monkeypatch.setattr(process_tree, "os", windows_os)
    commands = []
    monkeypatch.setattr(process_tree.subprocess, "run", lambda argv, **_kw: commands.append(list(argv)))

    process_tree.stop_process_tree(_Child(), grace_seconds=0.01)

    assert commands == [[TASKKILL, "/PID", "4242", "/T", "/F"]]
