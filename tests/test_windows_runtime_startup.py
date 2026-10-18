"""The API server and the native window must start on every platform the bundle ships to.

Measured on Windows 11 (2026-10-18): the API server died in `main()` on
`faulthandler.register(signal.SIGUSR1, ...)`, which Windows does not have, and the window
host died installing termination handlers because Windows has no `signal.SIGHUP`. Linux CI
never takes those branches and the Windows gauntlet never launched either process, so both
crashes shipped green. These tests run the real start paths on whatever platform runs them,
and the Windows gauntlet runs them on Windows.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _isolated_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"VOOL_HOME": str(home), "NULLA_HOME": str(home), "PYTHONUTF8": "1"})
    return env


def test_the_api_server_starts_and_answers_healthz_on_this_platform(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, str(REPO / "apps" / "vool_api_server.py"), "--port", str(port), "--bind", "127.0.0.1"],
        cwd=REPO, env=_isolated_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 90
        status = None
        while time.monotonic() < deadline and server.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                    status = response.status
                    break
            except OSError:
                time.sleep(0.5)
        if status is None:
            server.kill()
            _, err = server.communicate(timeout=30)
            raise AssertionError(f"API server never answered /healthz (exit={server.returncode}):\n{err.decode(errors='replace')[-4000:]}")
        assert status == 200
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=30)


def test_window_termination_signals_exist_on_this_platform() -> None:
    sys.path.insert(0, str(REPO))
    from installer.bundle import vool_window

    assert vool_window._TERMINATION_SIGNALS, "the host must still catch SIGTERM/SIGINT"
    for sig in vool_window._TERMINATION_SIGNALS:
        assert isinstance(sig, signal.Signals)
    assert {signal.SIGTERM, signal.SIGINT} <= set(vool_window._TERMINATION_SIGNALS)


def test_the_non_posix_handler_lane_installs_without_raising(monkeypatch) -> None:
    """The Windows lane, driven on any platform: it registers only real signals and never raises."""
    sys.path.insert(0, str(REPO))
    from installer.bundle import vool_window

    registered: list[signal.Signals] = []
    monkeypatch.setattr(vool_window, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(vool_window.signal, "signal", lambda sig, handler: registered.append(sig))

    vool_window._install_termination_handlers(object(), {"pid": None})

    assert registered == list(vool_window._TERMINATION_SIGNALS)
