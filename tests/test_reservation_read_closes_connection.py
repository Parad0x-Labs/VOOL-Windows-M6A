"""Read-only reservation inspection releases its owned SQLite handle on every exit."""
from __future__ import annotations

import sqlite3

import pytest

from core import effect_budget as budget


@pytest.mark.parametrize("failure", ["none", "schema", "query"])
def test_reservation_read_closes_its_connection(tmp_path, monkeypatch, failure):
    error = RuntimeError("synthetic reservation read failure")

    class ObservedConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, parameters=()):
            if failure == "query" and sql.lstrip().startswith("SELECT"):
                raise error
            return super().execute(sql, parameters)

        def close(self):
            self.closed = True
            return super().close()

    connection = sqlite3.connect(tmp_path / "reservations.db", factory=ObservedConnection)
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(budget, "_budget_connection", lambda: connection)
    if failure == "schema":
        def fail_schema(_connection):
            raise error

        monkeypatch.setattr(budget, "_ensure_tables", fail_schema)
    try:
        if failure == "none":
            assert budget.reservation_rows(owner_ref="synthetic-owner") == []
        else:
            with pytest.raises(RuntimeError) as raised:
                budget.reservation_rows(owner_ref="synthetic-owner")
            assert raised.value is error
        assert connection.closed, "the reservation reader retained its owned store handle"
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(connection, "SELECT 1")
    finally:
        connection.close()
