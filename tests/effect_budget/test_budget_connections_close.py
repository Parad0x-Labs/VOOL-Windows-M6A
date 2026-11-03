"""Every budget operation closes the store connection it opened, before it returns.

The budget module opened a fresh connection per operation and left it for the garbage collector.
CPython's sqlite3 connections sit in reference cycles, so they stay open until a cyclic collection
runs. On Linux that only holds a file descriptor; on Windows an open connection holds the database
file open without delete sharing, so a store directory could not be removed afterwards (Windows suite
41490181963: 17 tearDowns failed with WinError 32 on a budget connection opened by
release_unconsumed_for_owner -> reservation_rows, 3 more via active_budgets). Each budget operation now
closes what it opened, whether it returns, raises, or refuses.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.effect_budget.conftest import *  # noqa: F403 — budget fixtures


@pytest.fixture()
def opened(monkeypatch):
    """Every connection the budget store opens during the test, in order."""
    import storage.db as db

    connections: list[sqlite3.Connection] = []
    real = db.get_connection

    def tracking(*args, **kwargs):
        conn = real(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(db, "get_connection", tracking)
    return connections


def _still_open(connections: list[sqlite3.Connection]) -> list[int]:
    left = []
    for index, conn in enumerate(connections):
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # closed
        left.append(index)
    return left


IDENTITY = {"turn_id": "t-close", "session_id": "s-close", "project_key": "p-close"}


def _operations(eb):
    def reserve_consume_release():
        first = eb.reserve_effect_units("command", **IDENTITY)
        eb.bind_reservation_effect(first.reservation_id, "effect-close-1")
        eb.consume_reservation(first.reservation_id)
        second = eb.reserve_effect_units("command", effect_id="effect-close-2", **IDENTITY)
        eb.release_reservation(second.reservation_id)

    return {
        "reserve, bind, consume, release": reserve_consume_release,
        "active_budgets": lambda: eb.active_budgets(),
        "budget_status": lambda: eb.budget_status("command", **IDENTITY),
        "reservation_rows": lambda: eb.reservation_rows(),
        "budget_events": lambda: eb.budget_events(),
        "effect_reservation_states": lambda: eb.effect_reservation_states("effect-close-x"),
        "release_effect_reservations": lambda: eb.release_effect_reservations("effect-close-x"),
        "reconcile_stale_reservations": lambda: eb.reconcile_stale_reservations(force=True),
    }


@pytest.mark.parametrize(
    "name",
    [
        "reserve, bind, consume, release",
        "active_budgets",
        "budget_status",
        "reservation_rows",
        "budget_events",
        "effect_reservation_states",
        "release_effect_reservations",
        "reconcile_stale_reservations",
    ],
)
def test_a_budget_operation_closes_every_connection_it_opened(set_budget, opened, name):
    from core import effect_budget as eb

    set_budget(("command", eb.SCOPE_SESSION, 5))
    opened.clear()  # the fixture's own grant and adjustment are not under test here
    _operations(eb)[name]()
    assert opened, f"{name} opened no connection through the store seam"
    assert _still_open(opened) == [], f"{name} left connections open: {_still_open(opened)} of {len(opened)}"


def test_a_refused_reservation_still_closes_its_connection(set_budget, opened):
    from core import effect_budget as eb

    set_budget(("command", eb.SCOPE_SESSION, 1))
    eb.reserve_effect_units("command", **IDENTITY)
    opened.clear()
    with pytest.raises(eb.EffectBudgetRefusedError):
        eb.reserve_effect_units("command", **IDENTITY)
    assert opened and _still_open(opened) == []
