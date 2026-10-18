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
