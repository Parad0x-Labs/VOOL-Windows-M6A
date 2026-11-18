"""Off Windows, the 0.7.3 Window Fixes carries 7, 10 and 15 change nothing (owner rule, 16 Nov: Windows-only fixes).

- Carry 7: the A8 sweep steps read the forget authority per row, as Mac ships (no revocation._one_reading).
- Carry 10: every namespace schema ensure runs, inside a memory listing too.
- Carry 15: the bundle supervisor writes no start lines, passes no shown-mark path, and never restarts a running
  window host on a reopen request.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor


def test_the_flags_follow_the_real_os() -> None:
    from core import context_namespace, finalization

    assert finalization._SWEEP_ONE_READING is (os.name == "nt")
    assert context_namespace._SCHEMA_ONCE_ON is (os.name == "nt")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from core import runtime_paths
    from core.memory.revocation import initialize_memory_home

    home = tmp_path / "home"
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(home / "workspace"))
    runtime_paths.configure_runtime_home(home)
    initialize_memory_home(home)
    try:
        yield home
    finally:
        runtime_paths.configure_runtime_home(None)


@pytest.mark.parametrize("step", ["conversation_log", "memory_jsonl"])
def test_off_windows_the_sweep_steps_take_no_one_reading(home, monkeypatch, step) -> None:
    from core import finalization
    from core.memory import revocation

    monkeypatch.setattr(finalization, "_SWEEP_ONE_READING", False)
    entered: list[int] = []

    @contextlib.contextmanager
    def spy():
        entered.append(1)
        yield

    monkeypatch.setattr(revocation, "_one_reading", spy)
    if step == "conversation_log":
        assert finalization._sweep_step_conversation_log("req:none", "0" * 64) == finalization._SWEEP_STEP_OK
    else:
        assert finalization._sweep_step_memory_jsonl("req:none") == finalization._SWEEP_STEP_OK
    assert entered == []


@pytest.mark.parametrize("step", ["conversation_log", "memory_jsonl"])
def test_on_windows_the_sweep_steps_take_one_reading(home, monkeypatch, step) -> None:
    from core import finalization
    from core.memory import revocation

    monkeypatch.setattr(finalization, "_SWEEP_ONE_READING", True)
    entered: list[int] = []

    @contextlib.contextmanager
    def spy():
        entered.append(1)
        yield

    monkeypatch.setattr(revocation, "_one_reading", spy)
    if step == "conversation_log":
        finalization._sweep_step_conversation_log("req:none", "0" * 64)
    else:
        finalization._sweep_step_memory_jsonl("req:none")
    assert entered == [1]


def _count_ensures(monkeypatch) -> list[int]:
    from core import context_namespace as cn

    ran: list[int] = []
    real = cn._ensure_context_namespace_schema

    def counting():
        ran.append(1)
        return real()

    monkeypatch.setattr(cn, "_ensure_context_namespace_schema", counting)
    return ran


def test_off_windows_every_schema_ensure_runs_inside_a_scope(home, monkeypatch) -> None:
    from core import context_namespace as cn

    monkeypatch.setattr(cn, "_SCHEMA_ONCE_ON", False)
    ran = _count_ensures(monkeypatch)
    with cn._schema_once():
        for _ in range(4):
            cn.ensure_context_namespace_schema()
    assert len(ran) == 4


def test_on_windows_a_scope_ensures_the_schema_once(home, monkeypatch) -> None:
    from core import context_namespace as cn

    monkeypatch.setattr(cn, "_SCHEMA_ONCE_ON", True)
    ran = _count_ensures(monkeypatch)
    with cn._schema_once():
        for _ in range(4):
            cn.ensure_context_namespace_schema()
    assert len(ran) == 1


def _supervisor(tmp_path: Path) -> BundleSupervisor:
    root = tmp_path / "bundle"
    root.mkdir(exist_ok=True)
    home = tmp_path / "home"
    return BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})


def _child(pid: int) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None
    return child


def _poll(supervisor: BundleSupervisor, popen: mock.Mock, now: list[float]) -> None:
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(bundle_supervisor, "_is_windows_platform", return_value=False), \
            mock.patch.object(supervisor, "_healthy", return_value=True), \
            mock.patch.object(supervisor, "_last_measured", side_effect=lambda: now[0]):
        supervisor._reap_window()
        supervisor._consume_window_reopen()
        supervisor._recreate_hidden_window()
        supervisor._ensure_window()


def _launches(popen: mock.Mock) -> list:
    return [call for call in popen.call_args_list if str(call.args[0][-1]).endswith("vool_window.py")]


def test_off_windows_the_supervisor_logs_no_start_line_and_passes_no_shown_path(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(501), _child(502)])
    now = [1000.0]
    try:
        _poll(running, popen, now)
        launches = _launches(popen)
        assert len(launches) == 1
        assert "VOOL_WINDOW_SHOWN_PATH" not in launches[0].kwargs["env"]
        running.handles["window"].flush()
        window_log = running.log_dir / "window.log"
        assert "bundle supervisor started" not in (window_log.read_text(encoding="utf-8") if window_log.exists() else "")
        sup_log = running.log_dir / "bundle-supervisor.log"
        assert "window started pid=" not in (sup_log.read_text(encoding="utf-8") if sup_log.exists() else "")
    finally:
        running._stop_child("window")


def test_off_windows_a_reopen_never_restarts_a_running_host(tmp_path) -> None:
    running = _supervisor(tmp_path)
    running._prepare()
    popen = mock.Mock(side_effect=[_child(601), _child(602)])
    now = [1000.0]
    try:
        _poll(running, popen, now)
        now[0] += bundle_supervisor._HIDDEN_WINDOW_GRACE_SECONDS * 4
        running._request_window_reopen()
        _poll(running, popen, now)
        _poll(running, popen, now)
        assert len(_launches(popen)) == 1
        assert running.processes["window"].pid == 601
    finally:
        running._stop_child("window")
