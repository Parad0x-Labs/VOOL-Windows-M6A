from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from storage.db import active_default_db_path, get_connection

_T = TypeVar("_T")

#: What a certification row now DECIDES. It decided nothing when this store was written --
#: every row went out stamped ``routing_effect: "none"``, which stopped being true the moment
#: `core.final_answer_authorship` started reading these rows to answer "may this model write
#: the final answer?". A receipt that describes its own effect wrongly is the failure mode this
#: project keeps paying for, so the string is here, beside the rows, and named once.
CERTIFICATION_ROUTING_EFFECT = "final_answer_authorship"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS local_model_tool_certification_runs (
    run_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    state TEXT NOT NULL,
    successful INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    regression_confirmed INTEGER NOT NULL DEFAULT 0,
    fingerprint_json TEXT NOT NULL DEFAULT '{}',
    stages_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    latency_ms REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    observe_only INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_local_model_tool_certification_fingerprint
ON local_model_tool_certification_runs(fingerprint, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_local_model_tool_certification_model
ON local_model_tool_certification_runs(provider_name, model_name, started_at DESC);
"""


#: Databases whose certification table this process has set up. Measured 2026-10-21 (CI run 39637534766, tests
#: (4)): the setup ran as a bare executescript on every call, and POST /api/model-tool-certification/run answered
#: 500 "database schema has changed" while another connection committed a schema change.
_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def _init_table(db_path: str) -> None:
    """Set up the certification table once per database path, under the database's write lock.

    BEGIN IMMEDIATE takes the write lock before any statement, so another connection's schema commit lands wholly
    before or after the setup instead of under it.
    """
    if db_path in _READY:
        return
    with _READY_LOCK:
        if db_path in _READY:
            return
        _create_table(db_path)
        _READY.add(db_path)


def _with_table(db_path: str | Path | None, work: Callable[[sqlite3.Connection], _T], *, write: bool = False) -> _T:
    """Run ``work`` on a connection to ``db_path`` (the active database when None), its certification table set up.

    A database deleted or replaced while VOOL runs (a reused path) loses the table the memo remembers: that path
    is set up again, once, and ``work`` runs again.
    """
    path = str(db_path) if db_path is not None else active_default_db_path()
    _init_table(path)
    try:
        return _run(path, work, write=write)
    except sqlite3.OperationalError as exc:
        if "no such table: local_model_tool_certification_runs" not in str(exc):
            raise
    with _READY_LOCK:
        _READY.discard(path)
    _init_table(path)
    return _run(path, work, write=write)


def _run(db_path: str, work: Callable[[sqlite3.Connection], _T], *, write: bool = False) -> _T:
    """Run ``work`` inside one transaction (BEGIN IMMEDIATE when it writes), committed at the end.

    Measured 2026-10-26 (#66 CI, tests (9)): an autocommit read beside this store answered "database schema has
    changed" while another connection committed a schema change; inside a transaction it does not.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        result = work(conn)
        if conn.in_transaction:
            conn.commit()
        return result
    finally:
        conn.close()


def _create_table(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # One statement at a time: executescript would commit the transaction above before running the script.
        for statement in _TABLE_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def begin_certification_run(
    *,
    run_id: str,
    fingerprint: str,
    fingerprint_payload: dict[str, Any],
    provider_name: str,
    model_name: str,
    adapter_type: str,
    db_path: str | Path | None = None,
) -> None:
    def work(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO local_model_tool_certification_runs (
                run_id, fingerprint, provider_name, model_name, adapter_type, state,
                fingerprint_json, started_at, observe_only
            ) VALUES (?, ?, ?, ?, ?, 'probing', ?, ?, 1)
            """,
            (
                run_id,
                fingerprint,
                provider_name,
                model_name,
                adapter_type,
                json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")),
                _utcnow(),
            ),
        )
        conn.commit()

    _with_table(db_path, work, write=True)


def complete_certification_run(
    *,
    run_id: str,
    fingerprint: str,
    state: str,
    successful: bool,
    stages: dict[str, Any],
    evidence: dict[str, Any],
    latency_ms: float,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        previous = conn.execute(
            """
            SELECT state, successful, consecutive_failures
            FROM local_model_tool_certification_runs
            WHERE fingerprint = ? AND run_id != ? AND completed_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (fingerprint, run_id),
        ).fetchone()
        prior_verified = conn.execute(
            """
            SELECT 1 FROM local_model_tool_certification_runs
            WHERE fingerprint = ? AND run_id != ? AND state = 'verified'
            LIMIT 1
            """,
            (fingerprint, run_id),
        ).fetchone()
        consecutive_failures = 0
        if not successful:
            previous_failures = int(previous["consecutive_failures"] or 0) if previous else 0
            consecutive_failures = previous_failures + 1
        # Hysteresis is evidence, not authority: two consecutive failures after a verified run
        # confirm a regression, but neither this flag nor state is consumed by provider routing.
        regression_confirmed = bool(
            not successful and prior_verified is not None and consecutive_failures >= 2
        )
        conn.execute(
            """
            UPDATE local_model_tool_certification_runs
            SET state = ?, successful = ?, consecutive_failures = ?,
                regression_confirmed = ?, stages_json = ?, evidence_json = ?,
                latency_ms = ?, completed_at = ?
            WHERE run_id = ?
            """,
            (
                state,
                int(bool(successful)),
                consecutive_failures,
                int(regression_confirmed),
                json.dumps(stages, sort_keys=True, separators=(",", ":")),
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                max(0.0, float(latency_ms or 0.0)),
                _utcnow(),
                run_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM local_model_tool_certification_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return _row_payload(row) if row else {}

    return _with_table(db_path, work, write=True)


def latest_certification_run(
    *,
    provider_name: str,
    model_name: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    def work(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM local_model_tool_certification_runs
            WHERE provider_name = ? AND model_name = ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (provider_name, model_name),
        ).fetchone()
        return _row_payload(row) if row else None

    return _with_table(db_path, work)


def list_certification_runs(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM local_model_tool_certification_runs
            ORDER BY started_at DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_row_payload(row) for row in rows]

    return _with_table(db_path, work)


def _row_payload(row: Any) -> dict[str, Any]:
    payload = dict(row)
    decoded_names = {
        "fingerprint_json": "fingerprint_components",
        "stages_json": "stages",
        "evidence_json": "evidence",
    }
    for key, decoded_name in decoded_names.items():
        try:
            payload[decoded_name] = json.loads(payload.pop(key) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload[decoded_name] = {}
            payload.pop(key, None)
    payload["successful"] = bool(payload.get("successful"))
    payload["regression_confirmed"] = bool(payload.get("regression_confirmed"))
    # ``observe_only`` is a stored column from when the probe was diagnostic-only. It is now
    # read by the authorship authority, so the row reports the effect it actually has rather
    # than the one it had when the column was added.
    payload["observe_only"] = False
    payload["routing_effect"] = CERTIFICATION_ROUTING_EFFECT
    return payload


__all__ = [
    "CERTIFICATION_ROUTING_EFFECT",
    "begin_certification_run",
    "complete_certification_run",
    "latest_certification_run",
    "list_certification_runs",
]
