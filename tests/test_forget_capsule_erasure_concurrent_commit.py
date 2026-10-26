"""A commit by another connection while the capsule erase reads must not fail the forget.

CI 40361736417: a background writer (web0-task-poll's audit rows, the fact extractor's provider receipts)
committed between the erase's read and its first write; the deferred transaction's upgrade answered
SQLITE_BUSY_SNAPSHOT ("database is locked") at once, the chain stayed, and "Forget 26." could not be verified.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from core.active_context_capsule import erase_capsule_versions_for_forget


def _connect(path):
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class _CommitDuringRead:
    """The erase's own connection; once the chain has been read, another connection commits."""

    def __init__(self, conn, path):
        self.conn, self.path, self.writer = conn, path, None

    in_transaction = property(lambda self: self.conn.in_transaction)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def execute(self, sql, *args):
        cursor = self.conn.execute(sql, *args)
        if self.writer is None and sql.startswith("SELECT * FROM context_capsule_versions"):
            rows = cursor.fetchall()

            def write():
                other = _connect(self.path)
                try:
                    other.execute("INSERT INTO audit_log VALUES ('background')")
                    other.commit()
                finally:
                    other.close()

            self.writer = threading.Thread(target=write)
            self.writer.start()
            self.writer.join(0.2)
            return type("Rows", (), {"fetchall": lambda _self: rows})()
        return cursor


def test_a_commit_between_the_read_and_the_delete_does_not_leave_the_chain(tmp_path):
    path = str(tmp_path / "store.db")
    setup = _connect(path)
    setup.executescript(
        """
        CREATE TABLE context_capsule_versions (version_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
            mode TEXT NOT NULL, capsule_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE context_capsule_heads (chat_id TEXT PRIMARY KEY, version_id TEXT NOT NULL);
        CREATE TRIGGER context_capsule_versions_no_delete BEFORE DELETE ON context_capsule_versions
            BEGIN SELECT RAISE(ABORT, 'context capsule versions are immutable'); END;
        CREATE TABLE audit_log (event TEXT);
        """
    )
    setup.execute("INSERT INTO context_capsule_versions VALUES ('v1', 'chat', 'shadow', ?, 't')",
                  (json.dumps({"objective": {"text": "Remember that my locker is 26."}}),))
    setup.execute("INSERT INTO context_capsule_heads VALUES ('chat', 'v1')")
    setup.commit()
    setup.close()

    conn = _connect(path)
    racing = _CommitDuringRead(conn, path)
    try:
        assert erase_capsule_versions_for_forget(racing, chat_id="chat", token="26") == 1
        racing.writer.join()
        assert conn.execute("SELECT count(*) FROM context_capsule_versions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'context_capsule_versions_no_delete'").fetchone()
    finally:
        conn.close()
