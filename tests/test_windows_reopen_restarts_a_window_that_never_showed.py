"""A window host that runs but never shows its window is started again when VOOL is launched again.

Steve, 0.7.2 (e2532592, 15 Nov), at 3 to 5 GB free RAM: after a stop and a Start-menu relaunch the
window host stayed alive with only a hidden 300x300 form. window.log had nothing for that start, and
a second launch's "window reopen requested" left the hidden window as it was. Now every start leaves
a line in the child's own log, the host writes its pid to VOOL_WINDOW_SHOWN_PATH once its main window
is shown, and a reopen request that finds a host past its grace with no such mark starts it again.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from installer.bundle import bundle_supervisor, vool_window
from installer.bundle.bundle_supervisor import BundleSupervisor


def _supervisor(tmp_path: Path) -> BundleSupervisor:
    root = tmp_path / "bundle"
    root.mkdir(exist_ok=True)
    home = tmp_path / "home"
    return BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})


def _child(pid: int) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None
    return child


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0


def _poll(supervisor: BundleSupervisor, popen: mock.Mock, clock: _Clock) -> None:
    """One pass of the supervisor loop's window steps, with the API answering, at clock.now."""
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(supervisor, "_healthy", return_value=True), \
            mock.patch.object(supervisor, "_last_measured", side_effect=lambda: clock.now):
        supervisor._reap_window()
        supervisor._consume_window_reopen()
        supervisor._recreate_hidden_window()
        supervisor._ensure_window()


def _window_launches(popen: mock.Mock) -> list:
    return [call for call in popen.call_args_list if str(call.args[0][-1]).endswith("vool_window.py")]


def _started(tmp_path: Path):
    running = _supervisor(tmp_path)
    running._prepare()
    clock = _Clock()
    popen = mock.Mock(side_effect=[_child(301), _child(302), _child(303)])
    _poll(running, popen, clock)
    assert len(_window_launches(popen)) == 1
    return running, popen, clock


def _log(running: BundleSupervisor) -> str:
    return (running.log_dir / "bundle-supervisor.log").read_text(encoding="utf-8")


def test_every_window_start_is_written_in_window_log(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        running.handles["window"].flush()
        text = (running.log_dir / "window.log").read_text(encoding="utf-8")
        assert "bundle supervisor started window pid=301" in text
        assert "window started pid=301" in _log(running)
    finally:
        running._stop_child("window")


def test_the_window_host_is_told_where_to_mark_its_window_shown(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        env = _window_launches(popen)[0].kwargs["env"]
        assert env["VOOL_WINDOW_SHOWN_PATH"] == str(running.window_shown_path)
    finally:
        running._stop_child("window")


def test_a_reopen_restarts_a_host_that_never_showed_its_window(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS + 5
        running._request_window_reopen()
        _poll(running, popen, clock)
        launches = _window_launches(popen)
        assert len(launches) == 2
        assert running.processes["window"].pid == 302
        assert "window host pid=301 never showed its window" in _log(running)
        # The new start is logged too.
        running.handles["window"].flush()
        assert "started window pid=302" in (running.log_dir / "window.log").read_text(encoding="utf-8")
    finally:
        running._stop_child("window")


def test_a_reopen_leaves_a_window_that_was_shown(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        running.window_shown_path.write_text("301\n", encoding="utf-8")
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS + 5
        running._request_window_reopen()
        _poll(running, popen, clock)
        assert len(_window_launches(popen)) == 1
        assert running.processes["window"].pid == 301
        assert "never showed" not in _log(running)
    finally:
        running._stop_child("window")


def test_a_mark_left_by_an_earlier_host_does_not_count(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    running.window_shown_path.write_text("301\n", encoding="utf-8")   # left by an earlier start
    clock = _Clock()
    popen = mock.Mock(side_effect=[_child(401), _child(402)])
    try:
        _poll(running, popen, clock)
        assert not running.window_shown_path.exists()   # cleared before each start
        running.window_shown_path.write_text("301\n", encoding="utf-8")   # another pid's mark
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS + 5
        running._request_window_reopen()
        _poll(running, popen, clock)
        assert running.processes["window"].pid == 402
    finally:
        running._stop_child("window")


def test_a_young_host_gets_its_grace_then_is_restarted_if_still_hidden(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        clock.now += 5
        running._request_window_reopen()
        _poll(running, popen, clock)
        assert len(_window_launches(popen)) == 1   # a slow first start is not a hidden window
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS
        _poll(running, popen, clock)               # the same request, checked again
        assert len(_window_launches(popen)) == 2
    finally:
        running._stop_child("window")


def test_a_young_host_that_shows_its_window_within_the_grace_is_kept(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        clock.now += 5
        running._request_window_reopen()
        _poll(running, popen, clock)
        running.window_shown_path.write_text("301\n", encoding="utf-8")
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS
        _poll(running, popen, clock)
        assert len(_window_launches(popen)) == 1
    finally:
        running._stop_child("window")


def test_without_a_reopen_request_a_hidden_host_is_left_alone(tmp_path) -> None:
    running, popen, clock = _started(tmp_path)
    try:
        clock.now += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS * 4
        _poll(running, popen, clock)
        assert len(_window_launches(popen)) == 1
    finally:
        running._stop_child("window")


def test_the_host_writes_its_pid_when_its_window_is_shown(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "window-shown"
    monkeypatch.setenv("VOOL_WINDOW_SHOWN_PATH", str(marker))
    monkeypatch.setattr(vool_window, "_log", lambda message: None)

    class _Event:
        def __init__(self) -> None:
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

    window = mock.Mock()
    window.events.shown = _Event()
    assert vool_window._mark_shown_with(window) is True
    assert not marker.exists()                       # not before the window is shown
    for handler in window.events.shown.handlers:
        handler()
    assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_host_without_the_shown_hook_marks_itself_shown(tmp_path, monkeypatch) -> None:
    """Without the hook a shown window can't be told from a hidden one: never restart it."""
    marker = tmp_path / "window-shown"
    monkeypatch.setenv("VOOL_WINDOW_SHOWN_PATH", str(marker))
    monkeypatch.setattr(vool_window, "_log", lambda message: None)
    window = mock.Mock(spec=[])                      # no .events at all
    assert vool_window._mark_shown_with(window) is False
    assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_no_shown_path_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VOOL_WINDOW_SHOWN_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    vool_window._write_window_shown()
    assert list(tmp_path.iterdir()) == []
