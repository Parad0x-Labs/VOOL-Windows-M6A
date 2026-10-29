"""VOOL's Windows tree kills run Windows' own taskkill, never one found on PATH.

A bare ``taskkill`` is looked up through the app's folder, the current folder and PATH, so a
planted ``taskkill.exe`` there would run with VOOL's rights at every child stop. The helper
returns ``%SystemRoot%\\System32\\taskkill.exe`` only, and fails closed (OSError, which every
caller already answers by killing just its recorded child) when that path can't be trusted.
"""
from __future__ import annotations

import ntpath

import pytest

from core import process_liveness
from core.process_liveness import system_powershell, system_taskkill


def test_the_windows_folder_copy_is_returned(monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    seen: list[str] = []
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda p: seen.append(p) or True)
    assert system_taskkill() == ntpath.join("C:\\Windows", "System32", "taskkill.exe")
    assert seen == ["C:\\Windows\\System32\\taskkill.exe"]


@pytest.mark.parametrize("root", [None, "", "Windows", "..\\Windows", "\\Windows"])
def test_a_missing_or_relative_systemroot_fails_closed(monkeypatch, root) -> None:
    if root is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", root)
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda p: True)
    with pytest.raises(OSError):
        system_taskkill()


def test_a_missing_taskkill_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda p: False)
    with pytest.raises(OSError):
        system_taskkill()


def test_powershell_is_windows_powershell_from_the_windows_folder(monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    seen: list[str] = []
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda p: seen.append(p) or True)
    assert system_powershell() == "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    assert seen == ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"]


def test_powershell_fails_closed_like_taskkill(monkeypatch) -> None:
    monkeypatch.delenv("SystemRoot", raising=False)
    with pytest.raises(OSError):
        system_powershell()
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(process_liveness.os.path, "isfile", lambda p: False)
    with pytest.raises(OSError):
        system_powershell()
