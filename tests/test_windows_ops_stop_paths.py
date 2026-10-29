"""Two ops tools stopped a process with signals Windows does not have.

`ops/run_local_acceptance._stop_runtime` escalated to `signal.SIGKILL`, an AttributeError on
Windows, raised inside the `finally` that restores the operator's runtime. A wall-budget
overrun in `ops/local_ci_reproduction.execute_run` sent `SIGINT` with `Popen.send_signal`,
which raises ValueError on Windows (only SIGTERM, CTRL_C_EVENT and CTRL_BREAK_EVENT are
accepted there). Both are driven down their Windows behaviour on any platform.
"""
from __future__ import annotations

import os
import signal
import sys


class _NtOs:
    """`os` as this module sees it on Windows; pathlib elsewhere keeps the real platform."""

    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_stopping_a_runtime_that_outlives_sigterm_never_needs_sigkill(monkeypatch) -> None:
    from ops import run_local_acceptance

    clock = _Clock()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(run_local_acceptance.time, "time", clock.time)
    monkeypatch.setattr(run_local_acceptance.time, "sleep", clock.sleep)
    monkeypatch.setattr(run_local_acceptance, "_current_health",
                        lambda _base_url: {"ok": True, "runtime": {"pid": 4242}})
    monkeypatch.setattr(run_local_acceptance.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.delattr(signal, "SIGKILL", raising=False)

    run_local_acceptance._stop_runtime("http://127.0.0.1:11435")

    assert sent[0] == (4242, signal.SIGTERM)
    assert len(sent) == 2, sent


def test_a_wall_budget_overrun_stops_its_own_child_tree_on_windows(tmp_path, monkeypatch) -> None:
    import subprocess

    from ops import local_ci_reproduction

    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    (repo / "ops" / "pytest_watchdog.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    def _windows_send_signal(self, sig):
        if sig != signal.SIGTERM:
            raise ValueError(f"Unsupported signal: {sig}")
        if self.poll() is None:
            os.kill(self.pid, signal.SIGTERM)  # TerminateProcess on Windows

    taskkill_calls: list[list[str]] = []

    def _record_taskkill(command, **_kwargs):
        taskkill_calls.append(list(command))
        children[0].terminate()  # what taskkill /T /F does to this one-process tree

    children: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _remember(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    with monkeypatch.context() as windows:  # undone before pytest formats any failure
        windows.setattr(local_ci_reproduction, "os", _NtOs())
        windows.setattr(local_ci_reproduction.subprocess.Popen, "send_signal", _windows_send_signal)
        windows.setattr(local_ci_reproduction.subprocess.Popen, "kill",  # Windows: kill is terminate
                        local_ci_reproduction.subprocess.Popen.terminate)
        windows.setattr(local_ci_reproduction.subprocess, "Popen", _remember)
        windows.setattr(local_ci_reproduction.subprocess, "run", _record_taskkill)
        payload = local_ci_reproduction.execute_run(
            repo, tmp_path / "run", tmp_path / "profile", sys.executable,
            {"files": ["tests/test_nothing.py"]}, phase_timeout=60.0, wall_budget=0.5,
            node_budgets=False, extra_note=None,
        )

    assert payload["runner_interruption"] == "wall-budget-exceeded"
    assert taskkill_calls == [["taskkill", "/PID", str(children[0].pid), "/T", "/F"]]
    assert children[0].poll() is not None
