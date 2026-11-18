"""Windows: the goal store closes every connection its reads open, so the store can be removed.

The Mac 0.7.3 goal store's readers (``load_goal``, ``load_active_goal_for_session``,
``load_latest_goal_for_session``, ``reset_goal_state_for_tests``, ``reconcile_on_startup``) left
their connection to the garbage collector. On Windows a still-open handle keeps the database file
from being deleted (WinError 32), and 37 goal rows failed removing their temporary store. On a
Windows host each reader now closes its connection; off Windows it is left exactly as Mac ships it.
"""
from __future__ import annotations

import sqlite3

import pytest

from core import goal_workflow, windows_write_gate
from storage import db as storage_db
from storage.db import configure_default_db_path
from storage.migrations import run_migrations


def _is_open(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


@pytest.fixture()
def opened(tmp_path, monkeypatch):
    path = tmp_path / "goal-store.db"
    run_migrations(db_path=path)
    previous = configure_default_db_path(path)
    goal_workflow.reset_goal_state_for_tests()
    connections: list[sqlite3.Connection] = []
    real = storage_db.get_connection

    def recording(*args, **kwargs):
        conn = real(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(storage_db, "get_connection", recording)
    yield connections
    for conn in connections:
        conn.close()
    configure_default_db_path(previous)


def _read_everything() -> None:
    goal_workflow.load_goal("goal-missing")
    goal_workflow.load_active_goal_for_session("chat-a")
    goal_workflow.load_latest_goal_for_session("chat-a")
    goal_workflow.reconcile_on_startup()
    goal_workflow.reset_goal_state_for_tests()


def test_a_windows_host_closes_every_goal_store_read(opened, monkeypatch) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    _read_everything()
    assert len(opened) == 5
    assert [conn for conn in opened if _is_open(conn)] == []


def test_off_windows_the_goal_store_reads_are_left_as_mac_ships_them(opened, monkeypatch) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    _read_everything()
    assert len(opened) == 5
    assert all(_is_open(conn) for conn in opened)
