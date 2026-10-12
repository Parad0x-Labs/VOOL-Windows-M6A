"""Event writes reuse connections, never governance verdicts or write transactions."""
from __future__ import annotations

import contextlib
import sqlite3
import uuid

import pytest

from core import finalization as fin
from core import runtime_continuity as runtime
from storage import db
from storage.migrations import run_migrations


@pytest.fixture
def store(tmp_path):
    original = db.active_default_db_path()
    db.configure_default_db_path(tmp_path / "writer.db")
    runtime.configure_runtime_continuity_db_path(db.active_default_db_path())
    run_migrations()
    fin.reset_governance_readiness_for_tests()
    yield
    runtime.configure_runtime_continuity_db_path(original)
    db.configure_default_db_path(original)
    fin.reset_governance_readiness_for_tests()


def _govern(text):
    fid = str(uuid.uuid4())
    with contextlib.closing(db.get_connection()) as conn:
        conn.execute(
            "INSERT INTO a7_finalizations "
            "(finalization_id, content_hash, canonical_content, availability) VALUES (?, ?, ?, ?)",
            (fid, fin._sha256_hex(text), text, fin.AVAILABILITY_AVAILABLE),
        )
        conn.commit()
    return fid


def _track_connections(monkeypatch):
    opened = []
    original = fin.get_connection

    def tracked():
        conn = original()
        opened.append(conn)
        return conn

    monkeypatch.setattr(fin, "get_connection", tracked)
    return opened


def _assert_released(opened):
    assert fin._AVAILABILITY_READ_SCOPE.get() is None
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_event_write_uses_two_short_scopes_not_one_connection_per_string(store, monkeypatch):
    # Warm readiness so its separate schema probe is not counted as a payload read.
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = fin.writer_may_persist_text
    calls = []

    def observed(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(fin, "writer_may_persist_text", observed)
    details = {"rows": [f"step {n}" for n in range(40)]}
    event = runtime.append_runtime_event(
        session_id="writer-budget", event_type="task_progress", message="working", details=details,
    )
    assert event["rows"] == details["rows"]
    assert calls.count("working") == 2
    assert all(calls.count(text) == 2 for text in details["rows"])
    assert len(opened) == 2, f"{len(opened)} governance connections for one event write"
    _assert_released(opened)


@pytest.mark.parametrize("state", [fin.AVAILABILITY_WITHHELD, fin.AVAILABILITY_ERASED])
@pytest.mark.parametrize("boundary", ["preflight", "durable"])
def test_event_write_sees_committed_revocation_between_checks(store, monkeypatch, state, boundary):
    text = "writer revocation canary"
    fid = _govern(text)
    assert fin.writer_may_persist_text(text)
    opened = _track_connections(monkeypatch)
    original = fin.writer_may_persist_text
    calls = 0

    def revoke_after_allowed_check(value):
        nonlocal calls
        allowed = original(value)
        if value == text:
            calls += 1
            if calls == (1 if boundary == "preflight" else 3):
                assert allowed
                assert fin.set_availability(fid, state, reason="writer interleave")
        return allowed

    monkeypatch.setattr(fin, "writer_may_persist_text", revoke_after_allowed_check)
    event = runtime.append_runtime_event(
        session_id="writer-revocation", event_type="task_progress",
        message=text, details={"nested": [text]},
    )
    assert event["message"] == ""
    with contextlib.closing(db.get_connection()) as conn:
        row = conn.execute(
            "SELECT message, details_json FROM runtime_session_events WHERE session_id = ?",
            ("writer-revocation",),
        ).fetchone()
        assert row["message"] == ""
        assert text not in row["details_json"]
    _assert_released(opened)


@pytest.mark.parametrize("boundary", ["preflight", "durable"])
def test_event_write_refuses_governance_query_outage(store, monkeypatch, boundary):
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = fin.payload_availability_for_hash
    calls = 0

    def outage(digest):
        nonlocal calls
        calls += 1
        if calls >= (1 if boundary == "preflight" else 3):
            raise sqlite3.OperationalError("injected governance query outage")
        return original(digest)

    monkeypatch.setattr(fin, "payload_availability_for_hash", outage)
    event = runtime.append_runtime_event(
        session_id="writer-outage", event_type="task_progress",
        message="private message", details={"body": "private detail"},
    )
    assert event["message"] == ""
    assert not event.get("body")
    _assert_released(opened)


def test_event_write_releases_scopes_before_insert_failure(store, monkeypatch):
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = runtime._conn

    def refusing_insert():
        conn = original()

        def authorize(action, table, *unused):
            if action == sqlite3.SQLITE_INSERT and table == "runtime_session_events":
                # Neither read phase may leave its own connection alive for the write.
                _assert_released(opened)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(runtime, "_conn", refusing_insert)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.append_runtime_event(
            session_id="writer-insert-refusal", event_type="task_progress",
            message="working", details={"body": "detail"},
        )
    _assert_released(opened)
    with contextlib.closing(db.get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_session_events").fetchone()[0] == 0


def test_event_write_does_not_retain_governance_connections_between_operations(store, monkeypatch):
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    for n in range(2):
        runtime.append_runtime_event(
            session_id="writer-release", event_type="task_progress", message=f"step {n}",
        )
        _assert_released(opened)
    assert len(opened) == 4


def test_scope_rechecks_database_identity_and_releases_exception_paths(store, monkeypatch, tmp_path):
    original_path = db.active_default_db_path()
    second_path = tmp_path / "second.db"
    run_migrations(db_path=second_path)
    text = "second database withheld canary"
    with contextlib.closing(db.get_connection(second_path)) as conn:
        conn.execute(
            "INSERT INTO a7_finalizations "
            "(finalization_id, content_hash, canonical_content, availability) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), fin._sha256_hex(text), text, fin.AVAILABILITY_WITHHELD),
        )
        conn.commit()
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="injected scope failure"):
            with fin.availability_read_scope():
                assert fin.writer_may_persist_text(text)
                db.configure_default_db_path(second_path)
                assert not fin.writer_may_persist_text(text)
                db.configure_default_db_path(original_path)
                assert fin.writer_may_persist_text(text)
                raise RuntimeError("injected scope failure")
    finally:
        db.configure_default_db_path(original_path)
    _assert_released(opened)


def test_checkpoint_write_uses_two_short_scopes_not_one_connection_per_string(store, monkeypatch):
    details = {"rows": [f"source step {n}" for n in range(40)]}
    checkpoint = runtime.create_runtime_checkpoint(
        session_id="checkpoint-budget", request_text="working", source_context=details,
    )
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = fin.writer_may_persist_text
    calls = []

    def observed(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(fin, "writer_may_persist_text", observed)
    result = runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], final_response="answer")
    assert result["final_response"] == "answer"
    assert result["source_context"]["rows"] == details["rows"]
    assert calls.count("answer") == 2
    assert all(calls.count(text) == 2 for text in details["rows"])
    assert len(opened) == 2, f"{len(opened)} governance connections for one checkpoint write"
    _assert_released(opened)


@pytest.mark.parametrize("state", [fin.AVAILABILITY_WITHHELD, fin.AVAILABILITY_ERASED])
@pytest.mark.parametrize("boundary", ["preflight", "durable"])
def test_checkpoint_write_sees_committed_revocation_between_checks(store, monkeypatch, state, boundary):
    text = "checkpoint revocation canary"
    fid = _govern(text)
    checkpoint = runtime.create_runtime_checkpoint(
        session_id="checkpoint-revocation", request_text="working", source_context={"body": text},
    )
    assert fin.writer_may_persist_text(text)
    opened = _track_connections(monkeypatch)
    original = fin.writer_may_persist_text
    calls = 0

    def revoke_after_allowed_check(value):
        nonlocal calls
        allowed = original(value)
        if value == text:
            calls += 1
            if calls == (1 if boundary == "preflight" else 3):
                assert allowed
                assert fin.set_availability(fid, state, reason="checkpoint interleave")
        return allowed

    monkeypatch.setattr(fin, "writer_may_persist_text", revoke_after_allowed_check)
    with pytest.raises(runtime.CheckpointTransitionRefused):
        runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], final_response=text)
    _assert_released(opened)
    with contextlib.closing(db.get_connection()) as conn:
        row = conn.execute(
            "SELECT final_response FROM runtime_checkpoints WHERE checkpoint_id = ?",
            (checkpoint["checkpoint_id"],),
        ).fetchone()
        assert row["final_response"] == ""


@pytest.mark.parametrize("boundary", ["preflight", "durable"])
def test_checkpoint_write_refuses_governance_query_outage(store, monkeypatch, boundary):
    checkpoint = runtime.create_runtime_checkpoint(
        session_id="checkpoint-outage", request_text="working", source_context={"body": "detail"},
    )
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = fin.payload_availability_for_hash
    calls = 0

    def outage(digest):
        nonlocal calls
        calls += 1
        if calls >= (1 if boundary == "preflight" else 3):
            raise sqlite3.OperationalError("injected checkpoint governance outage")
        return original(digest)

    monkeypatch.setattr(fin, "payload_availability_for_hash", outage)
    with pytest.raises(runtime.CheckpointTransitionRefused):
        runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], final_response="answer")
    _assert_released(opened)
    assert runtime.get_runtime_checkpoint(checkpoint["checkpoint_id"])["final_response"] == ""


def test_checkpoint_write_releases_scopes_before_update_failure(store, monkeypatch):
    checkpoint = runtime.create_runtime_checkpoint(
        session_id="checkpoint-sql-refusal", request_text="working", source_context={"body": "detail"},
    )
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    original = runtime._conn

    def refusing_update():
        conn = original()

        def authorize(action, table, *unused):
            if action == sqlite3.SQLITE_UPDATE and table == "runtime_checkpoints":
                _assert_released(opened)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(runtime, "_conn", refusing_update)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], final_response="answer")
    _assert_released(opened)
    assert runtime.get_runtime_checkpoint(checkpoint["checkpoint_id"])["final_response"] == ""


def test_checkpoint_write_does_not_retain_governance_connections_between_operations(store, monkeypatch):
    checkpoint = runtime.create_runtime_checkpoint(
        session_id="checkpoint-release", request_text="working", source_context={"body": "detail"},
    )
    assert fin.writer_may_persist_text("warmup")
    opened = _track_connections(monkeypatch)
    for n in range(2):
        runtime.update_runtime_checkpoint(checkpoint["checkpoint_id"], final_response=f"answer {n}")
        _assert_released(opened)
    assert len(opened) == 4
