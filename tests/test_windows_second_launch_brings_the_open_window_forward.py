"""Opening VOOL again while its window is open (or minimized) brings that window to the front.

Capability test on 0.7.1-beta (A9): with VOOL running and its window minimized, the Start-menu
shortcut started a second bundle supervisor, which found the lease held, wrote the window-reopen
request and exited. The running supervisor leaves a window that is still open as it is
(_consume_window_reopen), so nothing reached the minimized window and the user saw nothing happen.
The second launch is the process the user just started, so it is the one that may hand the
foreground to the running window: it restores the open VOOL window and brings it forward.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor


class _FakeWindows:
    """Stands in for user32: top-level windows by pid, with their title and minimized state."""

    def __init__(self, windows: dict[int, list[tuple[int, str, bool]]]) -> None:
        self.windows = windows
        self.calls: list[tuple] = []

    def allow_foreground(self, pid: int) -> None:
        self.calls.append(("allow", pid))

    def windows_of(self, pid: int) -> list[int]:
        return [hwnd for hwnd, _title, _iconic in self.windows.get(pid, [])]

    def _find(self, hwnd: int) -> tuple[int, str, bool]:
        for entries in self.windows.values():
            for entry in entries:
                if entry[0] == hwnd:
                    return entry
        raise KeyError(hwnd)

    def title(self, hwnd: int) -> str:
        return self._find(hwnd)[1]

    def is_minimized(self, hwnd: int) -> bool:
        return self._find(hwnd)[2]

    def restore(self, hwnd: int) -> None:
        self.calls.append(("restore", hwnd))

    def show(self, hwnd: int) -> None:
        self.calls.append(("show", hwnd))

    def set_foreground(self, hwnd: int) -> None:
        self.calls.append(("foreground", hwnd))


def _supervisor(tmp_path: Path) -> BundleSupervisor:
    root = tmp_path / "bundle"
    root.mkdir(exist_ok=True)
    home = tmp_path / "home"
    return BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})


def _write_state(supervisor: BundleSupervisor, pids: dict) -> None:
    supervisor.run_dir.mkdir(parents=True, exist_ok=True)
    supervisor.state_path.write_text(json.dumps({"pids": pids}), encoding="utf-8")


def _second_launch(supervisor: BundleSupervisor, fake: _FakeWindows) -> int:
    supervisor._windows_factory = lambda: fake
    with mock.patch.object(bundle_supervisor, "_is_windows_platform", return_value=True), \
            mock.patch.object(supervisor, "_acquire_lease", return_value=False):
        return supervisor.run()


def test_a_second_launch_restores_and_raises_the_minimized_window(tmp_path) -> None:
    second = _supervisor(tmp_path)
    _write_state(second, {"api": 11, "window": 4242})
    fake = _FakeWindows({4242: [(77, "VOOL", True)], 11: [(99, "VOOL", True)]})

    assert _second_launch(second, fake) == 0

    assert second.window_reopen_path.exists()  # the reopen request is still made
    assert ("allow", 4242) in fake.calls
    assert ("restore", 77) in fake.calls
    assert ("foreground", 77) in fake.calls
    assert fake.calls.index(("restore", 77)) < fake.calls.index(("foreground", 77))
    assert all(call[-1] != 99 for call in fake.calls)  # only the window host's own window


def test_an_open_window_that_is_not_minimized_is_brought_forward_without_restore(tmp_path) -> None:
    second = _supervisor(tmp_path)
    _write_state(second, {"window": 5151})
    fake = _FakeWindows({5151: [(80, "VOOL", False)]})

    assert _second_launch(second, fake) == 0

    assert ("restore", 80) not in fake.calls
    assert ("show", 80) in fake.calls
    assert ("foreground", 80) in fake.calls


def test_only_the_vool_titled_window_of_the_host_is_raised(tmp_path) -> None:
    second = _supervisor(tmp_path)
    _write_state(second, {"window": 6161})
    fake = _FakeWindows({6161: [(81, "VOOL Companion", True), (82, "", False), (83, "VOOL", True)]})

    _second_launch(second, fake)

    touched = {call[1] for call in fake.calls if call[0] != "allow"}
    assert touched == {83}


def test_no_window_pid_in_the_state_touches_no_window(tmp_path) -> None:
    second = _supervisor(tmp_path)
    _write_state(second, {"api": 11})
    fake = _FakeWindows({11: [(99, "VOOL", True)]})

    assert _second_launch(second, fake) == 0

    assert fake.calls == []
    assert second.window_reopen_path.exists()


def test_an_unreadable_or_forged_state_touches_no_window(tmp_path) -> None:
    for state in ("not json", json.dumps({"pids": {"window": "4242"}}), json.dumps({"pids": {"window": -1}})):
        second = _supervisor(tmp_path)
        second.run_dir.mkdir(parents=True, exist_ok=True)
        second.state_path.write_text(state, encoding="utf-8")
        fake = _FakeWindows({4242: [(77, "VOOL", True)]})
        assert _second_launch(second, fake) == 0
        assert fake.calls == []


def test_off_windows_a_second_launch_touches_no_window(tmp_path) -> None:
    second = _supervisor(tmp_path)
    _write_state(second, {"window": 4242})
    fake = _FakeWindows({4242: [(77, "VOOL", True)]})
    second._windows_factory = lambda: fake
    with mock.patch.object(bundle_supervisor, "_is_windows_platform", return_value=False), \
            mock.patch.object(second, "_acquire_lease", return_value=False):
        assert second.run() == 0
    assert fake.calls == []
