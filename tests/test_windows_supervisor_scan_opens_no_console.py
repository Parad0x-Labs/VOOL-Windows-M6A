"""The Windows bundle supervisor starts no process that opens a console window.

Installed, the supervisor runs under pythonw.exe, a GUI program with no console of its own (VOOL.cmd
starts it with `start /B pythonw.exe`, and vool.vbs runs VOOL.cmd hidden "so no console window
flashes when VOOL starts"). A console program it starts gets a new console window unless it is
started with CREATE_NO_WINDOW. The API, Ollama, the window host and taskkill were. The PowerShell
scan for stale APIs, run before every API start, was not: its console (a Windows Terminal window,
where that is the default terminal) could flash at every launch and every API restart until
`-WindowStyle Hidden` took effect inside PowerShell.

These run off Windows with the platform seam pinned to Windows and every process start recorded;
nothing is started. CREATE_NO_WINDOW is given its Windows value where subprocess lacks it.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

_CREATE_NO_WINDOW = 0x08000000  # winbase.h
_CREATE_NEW_CONSOLE = 0x00000010  # with it, Windows ignores CREATE_NO_WINDOW


def _windowless(flags: int) -> bool:
    return bool(flags & _CREATE_NO_WINDOW) and not flags & _CREATE_NEW_CONSOLE


@pytest.fixture
def supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BundleSupervisor]:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor.run_dir.mkdir(parents=True, exist_ok=True)
    supervisor.log_dir.mkdir(parents=True, exist_ok=True)
    yield supervisor
    for handle in supervisor.handles.values():  # the children's log files
        with contextlib.suppress(OSError):
            handle.close()


def test_the_stale_api_scan_starts_powershell_without_a_console_window(supervisor: BundleSupervisor) -> None:
    runs: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        runs.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with mock.patch("installer.bundle.bundle_supervisor.subprocess.run", run):
        supervisor._clear_stale_api_processes()

    [(argv, kwargs)] = runs
    assert argv[0] == "powershell.exe"
    assert _windowless(int(kwargs.get("creationflags", 0)))


def test_every_process_the_supervisor_starts_on_windows_has_no_console_window(supervisor: BundleSupervisor) -> None:
    # Ollama, the scan and the API at launch, the window once the API answers, and taskkill when a
    # child is stopped: every one of them, so a new kind of process start cannot forget the flag.
    started: list[tuple[str, int]] = []

    def popen(argv: list[str], **kwargs: object) -> mock.Mock:
        started.append((Path(argv[0]).name, int(kwargs.get("creationflags", 0))))
        child = mock.Mock(pid=1001 + 2 * len(started))  # odd: no Windows process has this pid
        child.poll.return_value = None
        child.wait.return_value = 0
        return child

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        started.append((Path(argv[0]).name, int(kwargs.get("creationflags", 0))))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with (
        mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen),
        mock.patch("installer.bundle.bundle_supervisor.subprocess.run", run),
    ):
        with mock.patch.object(supervisor, "_healthy", return_value=False):
            supervisor._ensure_ollama()
            supervisor._ensure_api()
        with mock.patch.object(supervisor, "_healthy", return_value=True):
            supervisor._ensure_window()
        supervisor._stop_child("window")

    assert [program for program, _ in started] == [
        "ollama.exe",
        "powershell.exe",
        "python.exe",
        "pythonw.exe",
        "taskkill.exe",
    ]
    assert [program for program, flags in started if not _windowless(flags)] == []
