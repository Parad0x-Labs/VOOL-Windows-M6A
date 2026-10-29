"""An update that can't confirm the running VOOL stopped changes nothing and restarts nothing.

Swapping files under a live API leaves the old process serving: it answers the health check,
so the update would read "updated" while the old code still runs. When the stop can't be
confirmed, the update aborts before the swap, leaves the old install as it was, and starts
nothing (the running API is still up; the keep-alive watchdog resumes once the lock is gone).
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from core import process_liveness
from installer import self_update
from installer.self_update import run_update, sha256_of_file
from tests.test_self_update_updater import _downloader, _prepare


def _update(tmp_path, stop):
    project, home, wallet, zpath = _prepare(tmp_path)
    start = mock.Mock()
    result = run_update(
        target_version="v0.5.0",
        asset_url="https://example/vool-windows.zip",
        sha256_url="https://example/vool-windows.zip.sha256",
        project_root=project,
        vool_home=home,
        downloader=_downloader(zpath),
        sha256_fetcher=lambda url: sha256_of_file(zpath),
        stop_server=stop,
        start_server=start,
        health_check=lambda: True,
        now_fn=lambda: 1000.0,
    )
    return result, project, home, wallet, start


def test_update_aborts_before_the_swap_when_the_stop_is_unconfirmed(tmp_path) -> None:
    stop = mock.Mock(side_effect=self_update.ServerStillRunning("pid 4242 is still running"))

    result, project, home, wallet, start = _update(tmp_path, stop)

    assert result.ok is False and result.stage == "stop"
    assert "4242" in result.message
    assert "OLD 0.4.0" in (project / "apps" / "vool_api_server.py").read_text()
    assert (project / "core" / "oldmod.py").exists() and not (project / "core" / "newmod.py").exists()
    assert wallet.read_text() == "ENCRYPTED-WALLET-SEED"
    start.assert_not_called()
    assert not (project / self_update._UPDATING_LOCK_NAME).exists()  # the watchdog resumes
    recorded = json.loads((home / "data" / "update_result.json").read_text(encoding="utf-8"))
    assert recorded["ok"] is False and recorded["stage"] == "stop"


def test_any_stop_failure_aborts_the_update(tmp_path) -> None:
    """Unknown is not stopped: an error from the stop is treated as an unconfirmed stop."""
    result, project, _home, _wallet, start = _update(tmp_path, mock.Mock(side_effect=OSError("tasklist failed")))

    assert result.ok is False and result.stage == "stop"
    assert "OLD 0.4.0" in (project / "apps" / "vool_api_server.py").read_text()
    start.assert_not_called()


# --- _default_stop_server --------------------------------------------------------


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    path = home / "data" / "vool_api.pid"
    path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(self_update, "_STOP_WAIT_SECONDS", 0.2)
    return home, tmp_path, path


def _alive(monkeypatch, answers):
    """pid_alive answers in turn; the last answer repeats."""
    seen: list[int] = []

    def fake(pid):
        seen.append(pid)
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(process_liveness, "pid_alive", fake)
    return seen


def test_stop_returns_once_the_killed_server_is_gone(pidfile, monkeypatch) -> None:
    home, root, _path = pidfile
    _alive(monkeypatch, [True, False])
    monkeypatch.setattr(self_update, "_pid_is_python", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(self_update, "_terminate", lambda pid, *_: killed.append(pid) or True)

    self_update._default_stop_server(home, root)

    assert killed == [4242]


def test_stop_raises_when_the_killed_server_outlives_the_wait(pidfile, monkeypatch) -> None:
    home, root, _path = pidfile
    _alive(monkeypatch, [True])
    monkeypatch.setattr(self_update, "_pid_is_python", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(self_update, "_terminate", lambda pid, *_: killed.append(pid) or True)

    with pytest.raises(self_update.ServerStillRunning, match="4242"):
        self_update._default_stop_server(home, root)
    assert killed == [4242]


def test_stop_raises_without_killing_a_live_pid_it_cannot_confirm_is_vool(pidfile, monkeypatch) -> None:
    """Not confirmed python (another program, or the check could not run): never kill it, and
    never report the server stopped either."""
    home, root, _path = pidfile
    _alive(monkeypatch, [True])
    monkeypatch.setattr(self_update, "_pid_is_python", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(self_update, "_terminate", lambda pid, *_: killed.append(pid) or True)

    with pytest.raises(self_update.ServerStillRunning, match="4242"):
        self_update._default_stop_server(home, root)
    assert killed == []


def test_stop_is_done_when_the_pidfile_names_no_running_process(pidfile, monkeypatch) -> None:
    home, root, _path = pidfile
    _alive(monkeypatch, [False])
    killed: list[int] = []
    monkeypatch.setattr(self_update, "_terminate", lambda pid, *_: killed.append(pid) or True)

    self_update._default_stop_server(home, root)

    assert killed == []


def test_stop_raises_on_an_unreadable_pidfile(pidfile, monkeypatch) -> None:
    home, root, path = pidfile
    path.write_text("not-a-pid", encoding="utf-8")
    killed: list[int] = []
    monkeypatch.setattr(self_update, "_terminate", lambda pid, *_: killed.append(pid) or True)

    with pytest.raises(self_update.ServerStillRunning):
        self_update._default_stop_server(home, root)
    assert killed == []
