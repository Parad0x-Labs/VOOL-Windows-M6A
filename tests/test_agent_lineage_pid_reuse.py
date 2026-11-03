"""Agent lineage under pid reuse: Windows never reparents, so a stale ppid can name a newer process.

A process older than the one its ppid now names was not started by it and must never join that
agent's lineage (members can be signalled). On Windows an orphan keeps a dead parent's ppid instead
of moving to pid 1, so a parent that is gone or newer marks it as a stray.
"""

from __future__ import annotations

import pytest

from core.agent_team import lineage
from core.agent_team.lineage import AgentRoot, LineageTracker, ProcId


class _Proc:
    def __init__(self, pid: int, ppid: int, create_time: float) -> None:
        self.pid = pid
        self.info = {"pid": pid, "ppid": ppid, "create_time": create_time, "uids": None}

    def environ(self):
        return {}

    def cmdline(self):
        return [f"proc{self.pid}"]


ROOT = AgentRoot(agent_id="a1", root=ProcId(500, 100.0), token="", started_at=90.0)


def _scan(monkeypatch, procs):
    monkeypatch.setattr(lineage.psutil, "process_iter", lambda attrs=None: iter(procs))
    return LineageTracker(protected=[]).scan([ROOT])


def test_a_process_older_than_its_ppid_holder_never_joins_that_lineage(monkeypatch):
    procs = [_Proc(500, 1, 100.0), _Proc(600, 500, 50.0), _Proc(601, 500, 150.0)]

    lineages, _strays = _scan(monkeypatch, procs)

    assert sorted(lineages["a1"]) == [500, 601]
    assert lineages["a1"][601].depth == 1


def test_windows_orphan_with_a_vanished_parent_is_a_stray(monkeypatch):
    monkeypatch.setattr(lineage, "_IS_WINDOWS", True, raising=False)
    procs = [_Proc(500, 1, 100.0), _Proc(700, 9999, 120.0)]

    _lineages, strays = _scan(monkeypatch, procs)

    assert [s.pid for s in strays] == [700]


def test_windows_orphan_whose_parent_pid_was_reused_is_a_stray(monkeypatch):
    monkeypatch.setattr(lineage, "_IS_WINDOWS", True, raising=False)
    procs = [_Proc(500, 1, 100.0), _Proc(2, 0, 0.0), _Proc(800, 2, 130.0), _Proc(700, 800, 120.0)]

    lineages, strays = _scan(monkeypatch, procs)

    assert [s.pid for s in strays] == [700]
    assert 700 not in lineages["a1"]


@pytest.mark.parametrize("windows", [False, True])
def test_a_live_unrelated_parent_is_not_a_stray(monkeypatch, windows):
    monkeypatch.setattr(lineage, "_IS_WINDOWS", windows, raising=False)
    procs = [_Proc(500, 1, 100.0), _Proc(800, 1, 50.0), _Proc(700, 800, 120.0)]

    _lineages, strays = _scan(monkeypatch, procs)

    assert strays == []
