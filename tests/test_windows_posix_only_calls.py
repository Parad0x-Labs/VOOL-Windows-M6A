"""Runtime paths that named POSIX-only os/signal members crashed on Windows.

Measured on Windows 11 (2026-10-18). `os.chown` is absent there, and its AttributeError
escaped an OSError suppression, so overwriting any existing workspace file failed.
`signal.SIGKILL` is absent too, so cancelling a browser session raised instead of stopping
the engine. Each test removes the member to drive the Windows branch on any platform.
"""
from __future__ import annotations

import os
import queue
import signal

from core.execution.artifacts import atomic_write_text
from core.vool_browser import sessions


def test_an_atomic_write_leaves_exactly_the_reviewed_bytes(tmp_path) -> None:
    """Text mode on Windows rewrote every "\\n" as "\\r\\n", so a written file never matched the
    sha256 of the content that was reviewed, and every file VOOL edited changed its line endings."""
    import hashlib

    target = tmp_path / "module.py"
    for content in ("def f():\n    return 1\n", "crlf\r\nkept\r\n"):
        atomic_write_text(target, content)
        assert target.read_bytes() == content.encode("utf-8")
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_overwriting_an_existing_file_works_without_os_chown(tmp_path, monkeypatch) -> None:
    monkeypatch.delattr(os, "chown", raising=False)
    target = tmp_path / "notes.txt"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert not list(tmp_path.glob(".notes.txt.*.tmp")), "no temporary file may be left behind"


class _LiveThread:
    def is_alive(self) -> bool:
        return True

    def join(self, timeout=None) -> None:
        return None


def _cancellable_session(tmp_path):
    handle = sessions.SessionHandle.__new__(sessions.SessionHandle)
    handle.cancelled = False
    handle.state = "running"
    handle._pid = 4242
    handle._thread = _LiveThread()
    handle._queue = queue.Queue()
    handle.profile_dir = tmp_path / "profile"
    handle.profile_dir.mkdir()
    return handle


def test_cancel_stops_the_engine_where_sigkill_does_not_exist(tmp_path, monkeypatch) -> None:
    sent: list[int] = []
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    handle = _cancellable_session(tmp_path)

    handle.close(cancelled=True)

    assert sent == [(4242, signal.SIGTERM)]
    assert handle.state == "cancelled"


def test_cancel_still_escalates_to_sigkill_where_it_exists(tmp_path, monkeypatch) -> None:
    if not hasattr(signal, "SIGKILL"):
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    sent: list[int] = []
    monkeypatch.setattr(sessions.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    handle = _cancellable_session(tmp_path)

    handle.close(cancelled=True)

    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_a_bounded_ci_step_stops_its_own_child_without_posix_signals(tmp_path, monkeypatch) -> None:
    """`ops.local_ci_reproduction._run_bounded` crashed on Windows with
    `AttributeError: module 'signal' has no attribute 'SIGKILL'` the moment a bounded
    step exceeded its bound, so the runner died with a traceback and no receipt. On
    Windows the step's OWN child and its tree are stopped with terminate/kill plus
    taskkill /T /F; os.killpg (absent there) is never called."""
    import sys

    from ops import local_ci_reproduction

    def _no_killpg(*_args):
        raise AssertionError("os.killpg does not exist on Windows")

    taskkill_calls: list[list[str]] = []
    with monkeypatch.context() as windows:  # undone before pytest formats any failure
        windows.setattr(local_ci_reproduction.os, "name", "nt")
        windows.setattr(local_ci_reproduction.os, "killpg", _no_killpg, raising=False)
        windows.setattr(local_ci_reproduction.subprocess, "run",
                        lambda command, **_kwargs: taskkill_calls.append(list(command)))
        result = local_ci_reproduction._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path, env=dict(os.environ),
            stdout_path=tmp_path / "out.txt", stderr_path=tmp_path / "err.txt",
            timeout=0.5,
        )

    assert result["exit"] is None and result["timed_out"] is True
    assert result["stopped"] is True
    assert taskkill_calls == [["taskkill", "/PID", str(result["pid"]), "/T", "/F"]]


def _sleeping_own_child():
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                            start_new_session=os.name != "nt")


def _stop_a_rig_daemon_as_windows(monkeypatch, rig_module, daemon, attribute) -> None:
    """Drive a rig's stop() down its Windows branch on any platform. CTRL_BREAK_EVENT
    only exists on Windows, so SIGTERM stands in for it; taskkill is recorded."""
    child = getattr(daemon, attribute)
    taskkill_calls: list[list[str]] = []
    try:
        with monkeypatch.context() as windows:  # undone before pytest formats any failure
            windows.setattr(rig_module.os, "name", "nt")
            windows.delattr(rig_module.signal, "SIGKILL", raising=False)
            windows.setattr(rig_module.signal, "CTRL_BREAK_EVENT", signal.SIGTERM, raising=False)
            windows.setattr(rig_module.subprocess, "run",
                            lambda command, **_kwargs: taskkill_calls.append(list(command)))
            daemon.stop()
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=15)
    assert getattr(daemon, attribute) is None
    assert taskkill_calls == [["taskkill", "/PID", str(child.pid), "/T", "/F"]]


def test_the_served_skill_rig_stops_its_daemon_tree_without_posix_signals(tmp_path, monkeypatch) -> None:
    """`tests._served_skill_rig.ServedDaemon.stop` raised the same AttributeError on
    Windows, so the rig's OWN daemon was never stopped. On Windows it now stops the
    daemon and the processes it started (taskkill /T /F of that one pid)."""
    from tests import _served_skill_rig

    daemon = _served_skill_rig.ServedDaemon.__new__(_served_skill_rig.ServedDaemon)
    daemon.home = tmp_path
    daemon.proc = _sleeping_own_child()

    _stop_a_rig_daemon_as_windows(monkeypatch, _served_skill_rig, daemon, "proc")


def test_the_usepod_served_rig_stops_its_daemon_tree_without_posix_signals(monkeypatch) -> None:
    """`tests.usepod.test_usepod_served_flow.UsePodServedDaemon.stop` has the same
    Windows crash. Same fix: stop the rig's OWN daemon and the processes it started."""
    from tests.usepod import test_usepod_served_flow

    daemon = test_usepod_served_flow.UsePodServedDaemon.__new__(
        test_usepod_served_flow.UsePodServedDaemon)
    daemon.process = _sleeping_own_child()

    _stop_a_rig_daemon_as_windows(monkeypatch, test_usepod_served_flow, daemon, "process")
