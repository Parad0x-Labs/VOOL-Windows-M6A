"""The lineage scan asks psutil only for attributes this platform has.

T34's Windows suite (run 41969949799) failed
test_agent_team_model_loop.py::test_l1_parallel_agents_on_their_own_models_while_the_chat_keeps_its_own
with "ValueError: invalid attr name 'uids'": the scan requested ``uids`` from process_iter, psutil on
Windows has none, and the whole process-table pass raised. Windows has no uid (the owner filter is
already off there), so the scan must not ask for one; on POSIX a process another uid owns stays out.
"""
from __future__ import annotations

from collections import namedtuple

from core.agent_team import lineage
from core.agent_team.lineage import AgentRoot, LineageTracker, ProcId

_Uids = namedtuple("_Uids", "real effective saved")
_WINDOWS_ATTRS = frozenset({"pid", "ppid", "create_time", "name", "username", "cmdline", "environ"})


class _Proc:
    def __init__(self, pid: int, ppid: int, create_time: float, uid: int | None = None) -> None:
        self.pid = pid
        self._row = {"pid": pid, "ppid": ppid, "create_time": create_time, "uids": None if uid is None else _Uids(uid, uid, uid)}
        self.info: dict = {}

    def environ(self):
        return {}

    def cmdline(self):
        return [f"proc{self.pid}"]


ROOT = AgentRoot(agent_id="a1", root=ProcId(500, 100.0), token="", started_at=90.0)


def _process_iter(procs, available):
    def _iter(attrs=None):
        for name in attrs or ():
            if name not in available:
                raise ValueError(f"invalid attr name {name!r}")  # psutil's own refusal
        for proc in procs:
            proc.info = {name: proc._row.get(name) for name in attrs}
            yield proc
    return _iter


def test_a_windows_scan_never_asks_for_uids_and_still_finds_the_lineage(monkeypatch) -> None:
    monkeypatch.delattr(lineage.os, "getuid", raising=False)
    procs = [_Proc(500, 1, 100.0), _Proc(601, 500, 150.0)]
    monkeypatch.setattr(lineage.psutil, "process_iter", _process_iter(procs, _WINDOWS_ATTRS))

    lineages, strays = LineageTracker(protected=[]).scan([ROOT])

    assert sorted(lineages["a1"]) == [500, 601]
    assert strays == []


def test_on_posix_a_process_another_uid_owns_stays_out(monkeypatch) -> None:
    monkeypatch.setattr(lineage.os, "getuid", lambda: 1000, raising=False)
    procs = [_Proc(500, 1, 100.0, uid=1000), _Proc(601, 500, 150.0, uid=1000), _Proc(602, 500, 150.0, uid=0)]
    monkeypatch.setattr(lineage.psutil, "process_iter", _process_iter(procs, _WINDOWS_ATTRS | {"uids"}))

    lineages, _strays = LineageTracker(protected=[]).scan([ROOT])

    assert sorted(lineages["a1"]) == [500, 601]
