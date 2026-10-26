"""Stores that create their tables on first use set them up without running DDL under schema changes.

Measured 2026-10-21 and 2026-10-26 in CI (run 39637534766 tests (4); #66 tests (9)): a request answered 500
"database schema has changed". These stores ran their `CREATE ... IF NOT EXISTS` setup as bare autocommit DDL on
every call, and SQLite reports SQLITE_SCHEMA to such a statement -- and to a plain read beside it -- when another
connection commits a schema change under it, which every lazily created table does on its first use.

Each store's setup now runs inside BEGIN IMMEDIATE, once per database file per process, so a process's first use
waits for another connection's write lock instead of running DDL under it; later calls run no DDL at all; and a
database file deleted under a running process gets its tables back. Each case uses one of the store's read entry
points: a write would wait for the lock on the old code too.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

CHURN_SECONDS = 3.0

#: (module, read call on that module) for each store whose tables are created on first use.
STORES = [
    ("storage.cloud_model_catalog", "list_provider_catalog('lock-probe')"),
    ("storage.model_radar", "list_findings()"),
    ("core.provider_invocation_gateway", "load_provider_manifest('lock-probe')"),
    ("core.provenance_store", "load_manifest('lock-probe')"),
    ("core.model_spend_ledger", "get_spend_reservation('lock-probe')"),
    ("core.model_orchestration_state", "ModelOrchestrationStore().current('lock-probe')"),
    ("core.school.store", "get_first_school()"),
]

def _read(module_name: str, call: str):
    import importlib

    return eval(call, vars(importlib.import_module(module_name)))


@pytest.fixture
def store_db(tmp_path: Path):
    import storage.db as database

    previous = database.active_default_db_path()
    db_path = tmp_path / "store.db"
    database.configure_default_db_path(db_path)
    try:
        yield db_path
    finally:
        database.configure_default_db_path(previous)


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def _set_up(module_name: str) -> None:
    """Run the store's own table setup step, the code every one of its calls runs first."""
    import importlib

    module = importlib.import_module(module_name)
    if module_name == "storage.model_radar":
        conn = module._connect()
        try:
            module._init_tables(conn)
        finally:
            conn.close()
    elif module_name == "core.school.store":
        with module.school_db():
            pass
    elif module_name == "core.provider_invocation_gateway":
        module.ensure_provider_manifest_schema()
    elif module_name == "core.model_orchestration_state":
        module._init_tables()
    else:
        module._init_table()


@pytest.mark.parametrize("module_name,call", STORES, ids=[module for module, _ in STORES])
def test_table_setup_is_not_failed_by_another_connections_schema_change(
    module_name: str, call: str, store_db: Path
) -> None:
    _read(module_name, call)
    stop = time.monotonic() + CHURN_SECONDS
    failures: list[str] = []
    rounds: list[int] = []

    def churn() -> None:
        conn = sqlite3.connect(str(store_db), timeout=10.0, isolation_level=None)
        try:
            index = 0
            while time.monotonic() < stop:
                conn.execute(f"CREATE TABLE IF NOT EXISTS lazily_created_{index} (value TEXT)")
                index += 1
                time.sleep(0.0003)
        finally:
            conn.close()

    def caller() -> None:
        done = 0
        while time.monotonic() < stop:
            try:
                _set_up(module_name)
            except Exception as exc:  # any failure fails the case; the race shows as OperationalError
                failures.append(f"{type(exc).__name__}: {exc}")
            done += 1
        rounds.append(done)

    threads = [threading.Thread(target=churn)] + [threading.Thread(target=caller) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(rounds) >= 3, rounds
    assert not failures, f"{len(failures)} failed setups in {sum(rounds)}: {sorted(set(failures))}"


@pytest.mark.parametrize("module_name,call", STORES, ids=[module for module, _ in STORES])
def test_later_reads_in_the_process_do_not_set_up_the_tables_again(
    module_name: str, call: str, store_db: Path
) -> None:
    _read(module_name, call)
    holder = _hold_write_lock(store_db)
    try:
        started = time.monotonic()
        _read(module_name, call)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert elapsed < 2.0, f"a {module_name} read waited {elapsed:.1f}s on another connection's write lock"


@pytest.mark.parametrize("module_name,call", STORES, ids=[module for module, _ in STORES])
def test_a_database_deleted_under_the_process_gets_its_tables_back(
    module_name: str, call: str, store_db: Path
) -> None:
    _read(module_name, call)
    for suffix in ("", "-wal", "-shm"):
        Path(str(store_db) + suffix).unlink(missing_ok=True)
    _read(module_name, call)
