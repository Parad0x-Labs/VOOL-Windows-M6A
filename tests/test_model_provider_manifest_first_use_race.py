"""The model-manifest table is set up once per database, under the write lock, and again if it goes missing.

Measured 2026-10-21 in CI (run 39536779199, tests (9)): the first served certification after the daemon started
answered 500 "database schema has changed". `ModelRegistry().get_manifest` ran
`storage.model_provider_manifest._init_table` on every read, as bare autocommit CREATE TABLE / ALTER / CREATE INDEX
statements, while the fresh home was still creating its other tables. SQLite reports SQLITE_SCHEMA to such a
statement when another connection commits a schema change under it.

The setup now takes BEGIN IMMEDIATE first, so another connection's schema commit lands wholly before or after it,
and it runs once per database path. A database deleted or replaced under a running process gets its table back.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_FIRST_READ = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import storage.db as database
from storage.model_provider_manifest import get_provider_manifest
database.configure_default_db_path(sys.argv[2])
Path(sys.argv[3]).write_text("ready")
print("found" if get_provider_manifest("lock-probe", "probe:1") is not None else "missing")
"""


@pytest.fixture
def store(tmp_path: Path):
    import storage.db as database
    from storage.model_provider_manifest import ModelProviderManifest, upsert_provider_manifest

    previous = database.active_default_db_path()
    db_path = tmp_path / "store.db"
    database.configure_default_db_path(db_path)
    try:
        upsert_provider_manifest(
            ModelProviderManifest(
                provider_name="lock-probe", model_name="probe:1", source_type="http", adapter_type="openai_compatible"
            )
        )
        yield db_path
    finally:
        database.configure_default_db_path(previous)


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_a_processs_first_manifest_read_sets_up_the_table_under_the_write_lock(store: Path, tmp_path: Path) -> None:
    ready = tmp_path / "reader-ready"
    holder = _hold_write_lock(store)
    try:
        reader = subprocess.Popen(
            [sys.executable, "-B", "-c", _FIRST_READ, str(REPO_ROOT), str(store), str(ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 60.0
        while not ready.exists() and reader.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        # Well inside the reader's 5 s busy timeout, so a slow runner cannot turn a wait into a refusal.
        time.sleep(0.5)
        waited = reader.poll() is None
    finally:
        holder.execute("COMMIT")
        holder.close()
    out, err = reader.communicate(timeout=60)
    assert reader.returncode == 0, err[-2000:]
    assert out.strip() == "found", (out, err[-2000:])
    assert waited, "the first manifest read ran its table setup without the database's write lock"


def test_later_manifest_reads_in_the_process_do_not_set_up_the_table_again(store: Path) -> None:
    from storage.model_provider_manifest import get_provider_manifest

    holder = _hold_write_lock(store)
    try:
        started = time.monotonic()
        found = get_provider_manifest("lock-probe", "probe:1")
        elapsed = time.monotonic() - started
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert found is not None
    assert elapsed < 2.0, f"a read waited {elapsed:.1f}s on another connection's write lock"


def test_a_database_replaced_under_the_process_gets_its_manifest_table_back(store: Path) -> None:
    from storage.model_provider_manifest import (
        ModelProviderManifest,
        get_provider_manifest,
        list_provider_manifests,
        upsert_provider_manifest,
    )

    assert get_provider_manifest("lock-probe", "probe:1") is not None
    for suffix in ("", "-wal", "-shm"):
        Path(str(store) + suffix).unlink(missing_ok=True)

    assert get_provider_manifest("lock-probe", "probe:1") is None
    assert list_provider_manifests() == []
    upsert_provider_manifest(
        ModelProviderManifest(
            provider_name="after-replace", model_name="probe:2", source_type="http", adapter_type="openai_compatible"
        )
    )
    assert [m.provider_name for m in list_provider_manifests()] == ["after-replace"]
