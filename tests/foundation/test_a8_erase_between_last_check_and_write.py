"""An erase that completes between a runtime writer's last A8 check and its write cannot be undone.

The runtime writers re-check availability at the "durable boundary", but that re-check runs before
the first INSERT/UPDATE on the runtime connection, on another connection, holding no lock the
erase respects. So an erase could flip the payload to ERASED and finish its whole sweep in the gap,
and the writer would then store the erased bytes after the sweep had passed. The erase traversal
holds core.finalization.A8_TRAVERSAL_LOCK; a writer that holds it across check and commit is either
fully before the erase (and swept) or fully after it (and vetoed).
"""

from __future__ import annotations

import threading

import core.runtime_continuity as rc
from tests.foundation.test_a8_privacy_pass003 import _admit_finalize, _erase, a8_env


def _erase_inside_the_gap(monkeypatch, fid: str, payload_marker: str) -> dict:
    """Run the erase on another thread the first time the writer serializes a value carrying the
    payload, which is after its last veto and before its write. Waits a bounded time for it."""
    real = rc._json_dumps
    state: dict = {"fired": False, "erase": None}

    def erase():
        state["result"] = _erase(fid)

    def paused(value):
        if not state["fired"] and payload_marker in str(value):
            state["fired"] = True
            worker = threading.Thread(target=erase, name="a8-gap-eraser")
            worker.start()
            worker.join(timeout=3)
            state["erase"] = worker
        return real(value)

    monkeypatch.setattr(rc, "_json_dumps", paused)
    return state


def _stored(sql: str, args: tuple) -> list:
    conn = rc._conn()
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_an_event_cannot_land_erased_bytes_after_the_sweep(a8_env, monkeypatch):
    P = "gap event secret KILO-5150"
    fid = _admit_finalize(P)["finalization_id"]
    state = _erase_inside_the_gap(monkeypatch, fid, P)
    rc.append_runtime_event(session_id="s-gap", event_type="status", message="note", details={"quote": P})
    assert state["fired"], "the pause point was not reached"
    state["erase"].join(timeout=30)
    assert state["result"]["sweep_complete"]
    rows = _stored("SELECT message, details_json FROM runtime_session_events WHERE session_id = ?", ("s-gap",))
    assert all(P not in (row["message"] + row["details_json"]) for row in rows), (
        "RESURRECTION: erased bytes stored after the erase finished its sweep"
    )


def test_a_checkpoint_cannot_land_erased_bytes_after_the_sweep(a8_env, monkeypatch):
    P = "gap checkpoint secret LIMA-6260"
    fid = _admit_finalize(P)["finalization_id"]
    checkpoint = rc.create_runtime_checkpoint(session_id="s-gap", request_text="gap")
    state = _erase_inside_the_gap(monkeypatch, fid, P)
    try:
        rc.update_runtime_checkpoint(
            checkpoint["checkpoint_id"], source_context={"quote": P}, state={"step": 1}
        )
    except rc.CheckpointTransitionRefused:
        pass
    assert state["fired"], "the pause point was not reached"
    state["erase"].join(timeout=30)
    assert state["result"]["sweep_complete"]
    rows = _stored(
        "SELECT source_context_json, state_json, final_response FROM runtime_checkpoints WHERE checkpoint_id = ?",
        (checkpoint["checkpoint_id"],),
    )
    assert rows and all(P not in "".join(str(v) for v in row.values()) for row in rows), (
        "RESURRECTION: erased bytes stored after the erase finished its sweep"
    )
