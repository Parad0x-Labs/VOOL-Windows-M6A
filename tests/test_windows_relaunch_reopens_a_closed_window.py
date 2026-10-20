"""Launching VOOL again after closing its window shows the window again.

Closing the Windows window ends only vool_window.py: the bundle supervisor keeps the API and
Ollama running, the way the Mac app stays alive when its red X hides the window. The Mac app
shows that window again when the user reopens the app (docs/APPLE_CAPABILITY_MATRIX.md,
Close-to-Dock). On Windows the Start-menu shortcut started a second supervisor, which found the
lease held, logged "duplicate launch ignored" and exited, so no window came back until the
supervisor stopped (Step 1 VM test at a7caed3: no window within 300 s). A second launch now asks
the running supervisor, through a file in this home's run folder, to open the window again.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

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


def _poll(supervisor: BundleSupervisor, popen: mock.Mock) -> None:
    """One pass of the supervisor loop's window steps, with the API answering."""
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(supervisor, "_healthy", return_value=True):
        supervisor._reap_window()
        supervisor._consume_window_reopen()
        supervisor._ensure_window()


def _window_launches(popen: mock.Mock) -> int:
    return sum(1 for call in popen.call_args_list if str(call.args[0][-1]).endswith("vool_window.py"))


def test_a_second_launch_reopens_the_window_the_user_closed(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    assert running._acquire_lease()
    try:
        popen = mock.Mock(side_effect=[_child(101), _child(102)])
        _poll(running, popen)
        assert _window_launches(popen) == 1

        running.processes["window"].poll.return_value = 0  # the user closed the window
        _poll(running, popen)
        assert "window" not in running.processes
        assert _window_launches(popen) == 1  # closing stays closed on its own

        second = _supervisor(tmp_path)
        assert second.run() == 0  # the shortcut's launch: the lease is held, so it only asks
        assert running.window_reopen_path.exists()
        assert second.processes == {}

        _poll(running, popen)
        assert _window_launches(popen) == 2
        assert running.processes["window"].pid == 102
        assert not running.window_reopen_path.exists()
        log = (running.log_dir / "bundle-supervisor.log").read_text(encoding="utf-8")
        assert "window reopen requested" in log
    finally:
        running._stop_child("window")
        running._release_lease()


def test_a_second_launch_while_the_window_is_open_opens_no_second_window(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(201), _child(202)])
    _poll(running, popen)
    running._request_window_reopen()
    _poll(running, popen)
    assert _window_launches(popen) == 1
    assert running.processes["window"].pid == 201
    assert not running.window_reopen_path.exists()
    running._stop_child("window")


def test_a_launch_while_the_window_is_still_closing_reopens_it(tmp_path) -> None:
    # The user clicks X, then the shortcut, before the window process has finished exiting.
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(401), _child(402)])
    _poll(running, popen)
    running._request_window_reopen()
    _poll(running, popen)  # the window is still alive: the request waits for it
    assert _window_launches(popen) == 1
    running.processes["window"].poll.return_value = 0
    _poll(running, popen)
    assert _window_launches(popen) == 2
    assert running.processes["window"].pid == 402
    running._stop_child("window")


def test_closing_an_open_window_long_after_a_launch_keeps_it_closed(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(501), _child(502)])
    _poll(running, popen)
    running._request_window_reopen()
    _poll(running, popen)
    running.window_reopen_until = time.monotonic() - 1.0  # the grace period has passed
    running.processes["window"].poll.return_value = 0
    _poll(running, popen)
    assert _window_launches(popen) == 1
    assert "window" not in running.processes


def test_a_runtime_lost_window_still_reopens_without_a_launch(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(301), _child(302)])
    _poll(running, popen)
    running.processes["window"].poll.return_value = 3
    _poll(running, popen)
    assert _window_launches(popen) == 2
    running._stop_child("window")


def test_the_reopen_request_lives_in_this_homes_run_folder_not_the_program_folder(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)
    request = supervisor.window_reopen_path
    assert request.parent == supervisor.home / "run"
    assert supervisor.root not in request.parents


def test_a_reopen_request_left_from_before_does_not_outlive_a_fresh_start(tmp_path) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.run_dir.mkdir(parents=True)
    supervisor.window_reopen_path.write_text("reopen\n", encoding="utf-8")
    with mock.patch.object(supervisor, "_reap_dead"), mock.patch.object(supervisor, "_ensure_ollama"), \
            mock.patch.object(supervisor, "_ensure_api"), mock.patch.object(supervisor, "_ensure_window"), \
            mock.patch.object(supervisor, "_consume_window_reopen") as consume:
        assert supervisor.run(once=True) == 0
    assert not supervisor.window_reopen_path.exists()
    consume.assert_called_once()
