"""Routing shadow operations release their owned SQLite handles on every exit."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from core.routing_authority_v2 import SubjectBindingV2
from core.routing_authority_v2 import shadow_store as shadow


@pytest.fixture
def observed_database(tmp_path, monkeypatch):
    path = tmp_path / "shadow.sqlite3"
    connections = []
    failure = {"sql": "", "after_insert": False, "exit": False}
    error = RuntimeError("synthetic shadow-store failure")
    original_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if failure["sql"] and sql.lstrip().startswith(failure["sql"]):
                raise error
            result = super().execute(sql, parameters)
            if failure["after_insert"] and sql.lstrip().startswith("INSERT"):
                raise error
            return result

        def __exit__(self, kind, value, traceback):
            result = super().__exit__(kind, value, traceback)
            if failure["exit"]:
                raise error
            return result

    def connect(database, *args, **kwargs):
        if str(database) != str(path):
            return original_connect(database, *args, **kwargs)
        kwargs["factory"] = ObservedConnection
        connection = original_connect(database, *args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(shadow.sqlite3, "connect", connect)
    try:
        yield path, connections, failure, error, original_connect
    finally:
        for connection in connections:
            connection.close()


def _record():
    return SubjectBindingV2(
        authenticated_subject_id="subject-1",
        authenticated_session_id="session-1",
        turn_id="turn-1",
        context_digest="a" * 64,
        credential_generation=1,
    )


def _assert_closed(connections):
    assert connections, "the operation did not open its expected real SQLite connection"
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(connection, "SELECT 1")


def test_shadow_schema_initialization_closes_its_connection(observed_database):
    path, connections, _failure, _error, _connect = observed_database
    shadow.RoutingAuthorityV2ShadowStore(path)
    _assert_closed(connections)


@pytest.mark.parametrize("sql", ["PRAGMA busy_timeout", "CREATE TABLE"])
def test_failed_shadow_initialization_closes_and_preserves_the_exception(observed_database, sql):
    path, connections, failure, error, _connect = observed_database
    failure["sql"] = sql
    with pytest.raises(RuntimeError) as raised:
        shadow.RoutingAuthorityV2ShadowStore(path)
    assert raised.value is error
    _assert_closed(connections)


@pytest.mark.parametrize("operation", ["persist", "replay", "read", "missing", "list", "filtered", "empty"])
def test_shadow_operations_close_their_connections_including_early_returns(observed_database, operation):
    path, connections, _failure, _error, _connect = observed_database
    store = shadow.RoutingAuthorityV2ShadowStore(path)
    record = _record()
    if operation not in {"persist", "missing", "empty"}:
        store.persist_shadow_record(record)
    start = len(connections)
    if operation in {"persist", "replay"}:
        assert store.persist_shadow_record(record) == record.digest()
    elif operation == "read":
        assert store.read_shadow_record(record.digest()) == record
    elif operation == "missing":
        assert store.read_shadow_record("b" * 64) is None
    elif operation == "filtered":
        assert store.list_shadow_record_digests(record_type="RoutingPlanV2") == ()
    else:
        assert store.list_shadow_record_digests() == (() if operation == "empty" else (record.digest(),))
    _assert_closed(connections[start:])


@pytest.mark.parametrize("operation", ["persist", "read", "list"])
def test_failed_shadow_queries_close_and_preserve_the_exception(observed_database, operation):
    path, connections, failure, error, _connect = observed_database
    store = shadow.RoutingAuthorityV2ShadowStore(path)
    start = len(connections)
    failure["sql"] = "SELECT"
    with pytest.raises(RuntimeError) as raised:
        if operation == "persist":
            store.persist_shadow_record(_record())
        elif operation == "read":
            store.read_shadow_record("b" * 64)
        else:
            store.list_shadow_record_digests()
    assert raised.value is error
    _assert_closed(connections[start:])


def test_shadow_failure_rolls_back_before_releasing_its_connection(observed_database):
    path, connections, failure, error, original_connect = observed_database
    store = shadow.RoutingAuthorityV2ShadowStore(path)
    start = len(connections)
    failure["after_insert"] = True
    with pytest.raises(RuntimeError) as raised:
        store.persist_shadow_record(_record())
    assert raised.value is error
    with closing(original_connect(path)) as check:
        assert check.execute("SELECT COUNT(*) FROM routing_authority_v2_shadow_records").fetchone()[0] == 0
    _assert_closed(connections[start:])


def test_shadow_transaction_exit_failure_still_releases_the_connection(observed_database):
    path, connections, failure, error, _connect = observed_database
    store = shadow.RoutingAuthorityV2ShadowStore(path)
    start = len(connections)
    failure["exit"] = True
    with pytest.raises(RuntimeError) as raised:
        store.persist_shadow_record(_record())
    assert raised.value is error
    _assert_closed(connections[start:])


def test_shadow_file_can_be_removed_without_waiting_for_garbage_collection(observed_database):
    path, connections, _failure, _error, _connect = observed_database
    store = shadow.RoutingAuthorityV2ShadowStore(path)
    store.persist_shadow_record(_record())
    path.unlink()
    assert not path.exists()
    _assert_closed(connections)
