"""A store call is not failed by another connection's schema change.

Measured 2026-10-26 in CI (#66, tests (9)): the daemon's first POST /api/model-tool-certification/run answered
500 "database schema has changed" from a plain read in `storage.model_provider_manifest`. The table setup was
already once per database and under the write lock; the read after it ran as an autocommit statement, and
SQLite reports SQLITE_SCHEMA to such a statement when another connection commits a schema change under it --
which the stores that create their tables on first use do while VOOL serves.

Each call of the model-manifest and model-certification stores now runs its work inside one transaction
(BEGIN IMMEDIATE for the calls that write), so these cases call both stores' entry points from three threads
while another connection keeps committing new tables, and expect no error at all.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

CHURN_SECONDS = 4.0


@pytest.fixture
def db_path(tmp_path: Path):
    import storage.db as database
    from storage.model_provider_manifest import list_provider_manifests
    from storage.model_tool_certification_store import list_certification_runs

    previous = database.active_default_db_path()
    path = tmp_path / "store.db"
    database.configure_default_db_path(path)
    try:
        list_provider_manifests()
        list_certification_runs()
        yield path
    finally:
        database.configure_default_db_path(previous)


def _calls():
    from storage.model_provider_manifest import (
        ModelProviderManifest,
        get_provider_manifest,
        list_provider_manifests,
        upsert_provider_manifest,
    )
    from storage.model_tool_certification_store import (
        begin_certification_run,
        complete_certification_run,
        latest_certification_run,
        list_certification_runs,
    )

    manifest = ModelProviderManifest(
        provider_name="schema-probe", model_name="probe:1", source_type="http", adapter_type="openai_compatible"
    )

    def certify() -> None:
        run_id = uuid.uuid4().hex
        begin_certification_run(run_id=run_id, fingerprint="schema-probe", fingerprint_payload={},
                                provider_name="schema-probe", model_name="probe:1", adapter_type="openai_compatible")
        finished = complete_certification_run(run_id=run_id, fingerprint="schema-probe", state="verified",
                                              successful=True, stages={}, evidence={}, latency_ms=1.0)
        assert finished["run_id"] == run_id, finished

    return [
        ("get_provider_manifest", lambda: get_provider_manifest("schema-probe", "probe:1")),
        ("list_provider_manifests", list_provider_manifests),
        ("upsert_provider_manifest", lambda: upsert_provider_manifest(manifest)),
        ("begin/complete_certification_run", certify),
        ("latest_certification_run", lambda: latest_certification_run(provider_name="schema-probe", model_name="probe:1")),
        ("list_certification_runs", list_certification_runs),
    ]


def test_store_calls_survive_another_connection_committing_schema_changes(db_path: Path):
    calls = _calls()
    stop = time.monotonic() + CHURN_SECONDS
    failures: list[str] = []
    rounds: list[int] = []

    def churn() -> None:
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
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
            for name, call in calls:
                try:
                    call()
                except Exception as exc:  # any failure fails the case; the race shows as OperationalError
                    failures.append(f"{name}: {type(exc).__name__}: {exc}")
            done += 1
        rounds.append(done)

    threads = [threading.Thread(target=churn)] + [threading.Thread(target=caller) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(rounds) >= 3, rounds
    assert not failures, f"{len(failures)} failed store calls in {sum(rounds)} rounds: {sorted(set(failures))}"
