"""The forget-authority read of a memory store is not failed by another connection's schema change.

Measured 2026-10-27 while root-causing CI run 40441789238 (tests (0)): VoolMemory.revoked_tokens ran its revocation and
pending-erasure SELECT as a bare autocommit statement, and SQLite reports SQLITE_SCHEMA ("database schema has changed")
to such a statement when another connection commits a schema change under it. authorize_payload turned that error into
a refusal, so a served receipt silently left the packet: a correction's receipt went missing and the value it replaced
read as current. The read now runs inside a read transaction.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from core.vool_memory import VoolMemory
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")

CHURN_SECONDS = 3.0


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_MEMORY_RECEIPTS", "1")
    memory = VoolMemory(runtime_home=str(_profile(tmp_path)))
    yield memory
    memory.close()


@pytest.mark.parametrize("chat_id", ["revocation-read", None], ids=["one-chat", "every-chat"])
def test_the_revocation_read_is_not_failed_by_another_connections_schema_change(mem, chat_id):
    assert mem.revoked_tokens(chat_id, strict=True, include_pending=True) == ()
    stop = time.monotonic() + CHURN_SECONDS
    failures: list[str] = []
    rounds: list[int] = []

    def churn() -> None:
        conn = sqlite3.connect(str(mem.db_path), timeout=10.0, isolation_level=None)
        try:
            index = 0
            while time.monotonic() < stop:
                conn.execute(f"CREATE TABLE IF NOT EXISTS lazily_created_{index} (value TEXT)")
                conn.execute(f"DROP TABLE lazily_created_{index}")
                index += 1
        finally:
            conn.close()

    def reader() -> None:
        done = 0
        while time.monotonic() < stop:
            try:
                mem.revoked_tokens(chat_id, strict=True, include_pending=True)
            except Exception as exc:  # any failure fails the case; the race shows as OperationalError
                failures.append(f"{type(exc).__name__}: {exc}")
            done += 1
        rounds.append(done)

    threads = [threading.Thread(target=churn)] + [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(rounds) >= 3, rounds
    assert not failures, f"{len(failures)} failed reads in {sum(rounds)}: {sorted(set(failures))}"


def test_a_pending_erasure_is_still_refused_inside_the_read_transaction(mem):
    mem._conn.execute(
        "INSERT INTO memory_erasure_operations (operation_id, chat_id, token, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("op-pending", "revocation-read", "14", "pending", time.time()),
    )
    mem._conn.commit()
    with pytest.raises(RuntimeError, match="owner erasure is incomplete"):
        mem.revoked_tokens("revocation-read", strict=True, include_pending=True)
    assert not mem._conn.in_transaction
