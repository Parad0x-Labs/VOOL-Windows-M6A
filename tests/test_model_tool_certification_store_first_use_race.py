"""The certification table is set up once per database, under the write lock, and again if it goes missing.

Measured 2026-10-21 in CI (run 39637534766, tests (4)): POST /api/model-tool-certification/run answered 500
"database schema has changed" in tests/test_chat_startup_served.py. `storage.model_tool_certification_store`
ran `_ensure_table`, a bare `executescript` of CREATE TABLE / CREATE INDEX IF NOT EXISTS, on every call, while
another connection committed a schema change. SQLite reports SQLITE_SCHEMA to such a statement. It is the shard-9
race 3ae39e6 fixed for the model manifest table (tests/test_model_provider_manifest_first_use_race.py).

The setup now takes BEGIN IMMEDIATE first, so another connection's schema commit lands wholly before or after it,
and it runs once per database path, the default database's or an explicit `db_path`. A database deleted or replaced
under a running process gets its table back.
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
from storage.model_tool_certification_store import latest_certification_run
explicit = sys.argv[4] == "explicit"
if not explicit:
    database.configure_default_db_path(sys.argv[2])
Path(sys.argv[3]).write_text("ready")
row = latest_certification_run(provider_name="lock-probe", model_name="probe:1", db_path=sys.argv[2] if explicit else None)
print("found" if row is not None else "missing")
"""


def _begin(run_id: str, *, db_path: Path | None = None, model_name: str = "probe:1") -> None:
    from storage.model_tool_certification_store import begin_certification_run

    begin_certification_run(
        run_id=run_id,
        fingerprint=f"fp-{run_id}",
        fingerprint_payload={"run": run_id},
        provider_name="lock-probe",
        model_name=model_name,
        adapter_type="openai_compatible",
        db_path=db_path,
    )


@pytest.fixture
def store(tmp_path: Path):
    import storage.db as database

    previous = database.active_default_db_path()
    db_path = tmp_path / "store.db"
    database.configure_default_db_path(db_path)
    try:
        _begin("run-1")
        yield db_path
    finally:
        database.configure_default_db_path(previous)


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    holder.execute("BEGIN IMMEDIATE")
    return holder


@pytest.mark.parametrize("how", ["default", "explicit"])
def test_a_processs_first_certification_read_sets_up_the_table_under_the_write_lock(store: Path, tmp_path: Path, how: str) -> None:
    ready = tmp_path / "reader-ready"
    holder = _hold_write_lock(store)
    try:
        reader = subprocess.Popen(
            [sys.executable, "-B", "-c", _FIRST_READ, str(REPO_ROOT), str(store), str(ready), how],
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
    assert waited, "the first certification read ran its table setup without the database's write lock"


def test_later_certification_reads_in_the_process_do_not_set_up_the_table_again(store: Path) -> None:
    from storage.model_tool_certification_store import latest_certification_run, list_certification_runs

    holder = _hold_write_lock(store)
    try:
        started = time.monotonic()
        found = latest_certification_run(provider_name="lock-probe", model_name="probe:1")
        listed = list_certification_runs()
        elapsed = time.monotonic() - started
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert found is not None and [row["run_id"] for row in listed] == ["run-1"]
    assert elapsed < 2.0, f"a read waited {elapsed:.1f}s on another connection's write lock"


def test_an_explicit_database_gets_its_own_table(store: Path, tmp_path: Path) -> None:
    from storage.model_tool_certification_store import latest_certification_run, list_certification_runs

    other = tmp_path / "other.db"
    assert latest_certification_run(provider_name="lock-probe", model_name="probe:1", db_path=other) is None
    _begin("run-other", db_path=other)
    assert [row["run_id"] for row in list_certification_runs(db_path=other)] == ["run-other"]
    assert [row["run_id"] for row in list_certification_runs()] == ["run-1"]


def test_a_database_replaced_under_the_process_gets_its_certification_table_back(store: Path) -> None:
    from storage.model_tool_certification_store import latest_certification_run, list_certification_runs

    assert latest_certification_run(provider_name="lock-probe", model_name="probe:1") is not None
    for suffix in ("", "-wal", "-shm"):
        Path(str(store) + suffix).unlink(missing_ok=True)

    assert latest_certification_run(provider_name="lock-probe", model_name="probe:1") is None
    assert list_certification_runs() == []
    _begin("after-replace", model_name="probe:2")
    assert [row["run_id"] for row in list_certification_runs()] == ["after-replace"]
