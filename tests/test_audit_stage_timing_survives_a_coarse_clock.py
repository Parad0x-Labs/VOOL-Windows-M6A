"""Stage durations in the stepped audit must not depend on the platform's monotonic tick.

On Windows with Python 3.12, `time.monotonic()` is GetTickCount64 and ticks about every 15.6 ms,
so a stage that finishes inside one tick measured exactly 0.0 (U0 run 41492164336, shard 14:
"a real nomination call ran and must show non-zero time"). This drives the same audit as
tests/test_audit_detail_survives_the_request_in_durable_activity.py with the audit module's
monotonic clock frozen, standing in for a coarse tick, and requires a non-zero nomination time.
"""

from __future__ import annotations

import time as _real_time
from types import SimpleNamespace

import core.agent_runtime.stepped_audit as stepped_audit
from core.runtime_continuity import list_runtime_session_events
from tests.test_audit_detail_survives_the_request_in_durable_activity import _drive


def test_nomination_time_is_measured_below_one_monotonic_tick(monkeypatch) -> None:
    frozen = _real_time.monotonic()
    coarse = SimpleNamespace(**{name: getattr(_real_time, name) for name in dir(_real_time) if not name.startswith("__")})
    coarse.monotonic = lambda: frozen
    monkeypatch.setattr(stepped_audit, "time", coarse)

    session_id = "coarse-clock-1"
    _drive(session_id)
    events = list_runtime_session_events(session_id, limit=200)
    detail = [e for e in events if e.get("event_type") == "audit_candidate_detail"]
    assert detail, "no audit_candidate_detail event reached the durable store"
    timing = detail[-1].get("timing") or {}
    assert timing.get("nomination", 0) > 0, timing
    assert timing.get("total", 0) > 0, timing
