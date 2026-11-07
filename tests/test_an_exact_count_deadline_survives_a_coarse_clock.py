"""A walk deadline that is already due must stop the walk even when the clock has not ticked.

On Windows with Python 3.12, `time.monotonic()` is GetTickCount64 and ticks about every 15.6 ms.
With `max_seconds=0.0` the deadline equals the start time, and a strict `now > deadline` check
stays false until the next tick: T31 Windows suite 41918263493 read 277 chunks of an endless file
past its deadline. These drive the same walks as
tests/test_an_exact_count_walk_is_bounded_and_verifiable.py with the module's monotonic clock
frozen, standing in for a coarse tick, and require the due deadline to stop them at once.
"""

from __future__ import annotations

import os
import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.folder_overview as folder_overview
from core.folder_overview import _count_file_lines, measure_workspace_files


def _freeze_monotonic(monkeypatch: pytest.MonkeyPatch) -> float:
    frozen = _real_time.monotonic()
    coarse = SimpleNamespace(
        **{name: getattr(_real_time, name) for name in dir(_real_time) if not name.startswith("__")}
    )
    coarse.monotonic = lambda: frozen
    monkeypatch.setattr(folder_overview, "time", coarse)
    return frozen


def test_a_due_deadline_stops_an_endless_read_below_one_clock_tick(tmp_path: Path, monkeypatch):
    (tmp_path / "endless.py").write_text("x = 1\n", encoding="utf-8")
    state = {"calls": 0}

    def endless_read(fd, size):
        state["calls"] += 1
        if state["calls"] > 4_096:
            raise RuntimeError("reader consumed 4096 chunks past its deadline")
        return b"x" * min(size, 65_536)

    monkeypatch.setattr(folder_overview.os, "read", endless_read)
    _freeze_monotonic(monkeypatch)

    measurement = measure_workspace_files(str(tmp_path), "py", max_seconds=0.0)

    assert state["calls"] <= 64, f"the reader kept reading past its deadline ({state['calls']} chunks)"
    assert measurement.complete is False
    assert "time budget" in measurement.termination_reason, measurement.termination_reason


def test_a_due_deadline_stops_the_walk_below_one_clock_tick(tmp_path: Path, monkeypatch):
    for index in range(50):
        (tmp_path / f"g{index:02d}.py").write_text("x = 1\n", encoding="utf-8")
    _freeze_monotonic(monkeypatch)

    measurement = measure_workspace_files(str(tmp_path), "py", max_seconds=0.0)

    assert measurement.complete is False
    assert "time budget" in measurement.termination_reason, measurement.termination_reason
    assert measurement.count == 0, measurement.count


def test_a_reader_deadline_equal_to_now_is_due(tmp_path: Path, monkeypatch):
    big = tmp_path / "big.py"
    big.write_bytes(b"x = 1\n" * 100)
    frozen = _freeze_monotonic(monkeypatch)

    read = _count_file_lines(str(big), deadline=frozen)

    assert read.outcome == "time-budget", read.outcome
