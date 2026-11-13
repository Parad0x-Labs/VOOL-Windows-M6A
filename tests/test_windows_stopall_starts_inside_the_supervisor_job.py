"""/stopall on the installed Windows bundle actually stops VOOL.

Capability test on 0.7.1-beta (A11): ``/stopall`` answered "I could not start the shutdown just
now. Use the 'Stop VOOL' desktop shortcut..." while the Quit VOOL button worked. The bundle
supervisor starts the API inside a kill-on-close Job object that does not allow breakaway
(core/windows_job.py sets only JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE). spawn_detached_stop always asked
CreateProcess for CREATE_BREAKAWAY_FROM_JOB, which Windows refuses with ERROR_ACCESS_DENIED
(winerror 5) for a process in such a job; the OSError was swallowed and /stopall reported failure.
start_windows_detached already retries without breakaway on exactly that error; the stopper's own
launch did not. Under the bundle supervisor, /stopall now also writes the supervisor's own stop
signal in-process -- what the Quit VOOL button writes -- so the shutdown does not depend on a
child process outliving the job. The fallback text names something Windows really has.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core import vool_agent_brake
from installer import vool_stop

_BREAKAWAY = 0x01000000


def _access_denied() -> PermissionError:
    exc = PermissionError(13, "Access is denied")
    exc.winerror = 5
    return exc


def test_spawn_detached_stop_retries_without_breakaway_when_the_job_refuses_it(monkeypatch, tmp_path) -> None:
    calls: list[int] = []

    def _popen(cmd, **kwargs):
        calls.append(kwargs.get("creationflags", 0))
        if kwargs.get("creationflags", 0) & _BREAKAWAY:
            raise _access_denied()
        return object()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(vool_stop.subprocess, "Popen", _popen)

    assert vool_stop.spawn_detached_stop(project_root=tmp_path, vool_home=tmp_path / "home") is True
    assert len(calls) == 2
    assert calls[0] & _BREAKAWAY
    assert not calls[1] & _BREAKAWAY


def test_spawn_detached_stop_still_reports_any_other_failure(monkeypatch, tmp_path) -> None:
    def _popen(cmd, **kwargs):
        raise FileNotFoundError("python.exe")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(vool_stop.subprocess, "Popen", _popen)

    assert vool_stop.spawn_detached_stop(project_root=tmp_path, vool_home=tmp_path / "home") is False


def _bundle_home(monkeypatch, tmp_path: Path, *, supervisor_running: bool) -> Path:
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    if supervisor_running:
        (home / "run" / "bundle-supervisor.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_BUNDLE_ROOT", str(tmp_path / "bundle"))
    return home


def test_stopall_under_the_bundle_supervisor_writes_its_stop_signal(monkeypatch, tmp_path) -> None:
    home = _bundle_home(monkeypatch, tmp_path, supervisor_running=True)
    monkeypatch.setattr(vool_stop, "spawn_detached_stop", lambda **_kw: False)

    assert vool_agent_brake._default_stopall() is True
    assert (home / "run" / "stop").read_text(encoding="utf-8") == "stop\n"


def test_stopall_without_a_supervisor_does_not_claim_a_shutdown_it_did_not_start(monkeypatch, tmp_path) -> None:
    home = _bundle_home(monkeypatch, tmp_path, supervisor_running=False)
    monkeypatch.delenv("VOOL_BUNDLE_ROOT", raising=False)
    monkeypatch.setattr(vool_stop, "spawn_detached_stop", lambda **_kw: False)

    assert vool_agent_brake._default_stopall() is False
    assert not (home / "run" / "stop").exists()


@pytest.mark.parametrize("windows", (True, False))
def test_the_fallback_names_something_this_platform_has(monkeypatch, windows) -> None:
    monkeypatch.setattr(vool_agent_brake, "_is_windows", lambda: windows)
    vool_agent_brake.maybe_handle_agent_brake("/stopall", "s", stopall_fn=lambda: False)
    reply = vool_agent_brake.maybe_handle_agent_brake("/stopall confirm", "s", stopall_fn=lambda: False)
    assert reply is not None and reply["success"] is False
    text = reply["response"]
    if windows:
        assert "Stop VOOL" not in text
        assert "Quit VOOL" in text
    else:
        assert "Stop VOOL" in text


def test_the_windows_success_reply_names_the_start_menu(monkeypatch) -> None:
    monkeypatch.setattr(vool_agent_brake, "_is_windows", lambda: True)
    vool_agent_brake.maybe_handle_agent_brake("/stopall", "s", stopall_fn=lambda: True)
    reply = vool_agent_brake.maybe_handle_agent_brake("/stopall confirm", "s", stopall_fn=lambda: True)
    assert reply is not None and reply["success"] is True
    assert "Start menu" in reply["response"]
