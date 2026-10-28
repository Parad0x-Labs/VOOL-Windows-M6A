"""Set up a store's lazily created tables without running DDL under other connections' schema changes.

Many stores create their tables on first use with ``CREATE ... IF NOT EXISTS`` and used to run that script as
bare autocommit DDL on every call. Measured 2026-10-21 and 2026-10-26 (CI runs 39637534766 tests (4) and #66
tests (9)): such a statement answers "database schema has changed" when another connection commits a schema
change under it, and the first use of each lazily created table is exactly such a change.

``ensure_schema`` first looks the script's objects up in ``sqlite_master`` inside a read transaction, which
cannot be failed that way. Only when one is missing does it run the script, inside ``BEGIN IMMEDIATE``: the
write lock is taken before any statement, so another connection's schema commit lands wholly before or after
the setup, never under it. Because the lookup runs on every call, a database deleted, replaced or stripped of a
table while VOOL runs gets its tables back on the next call.
"""

from __future__ import annotations

import re
import sqlite3
from functools import cache
from pathlib import Path

from storage.db import active_default_db_path, get_connection

_OBJECT_NAME = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s+IF\s+NOT\s+EXISTS\s+[\"`\[]?(\w+)", re.IGNORECASE
)


@cache
def _object_names(script: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(_OBJECT_NAME.findall(script)))
    if not names:
        raise ValueError("a lazy schema script must create its objects with CREATE ... IF NOT EXISTS")
    return names


def _all_present(conn: sqlite3.Connection, names: tuple[str, ...]) -> bool:
    marks = ", ".join("?" for _ in names)
    conn.execute("BEGIN")
    try:
        found = conn.execute(f"SELECT COUNT(DISTINCT name) FROM sqlite_master WHERE name IN ({marks})", names).fetchone()
    finally:
        conn.rollback()
    return int(found[0]) == len(names)


def _set_up(conn: sqlite3.Connection, script: str) -> None:
    # executescript() commits any open transaction first, so BEGIN IMMEDIATE goes inside the script itself.
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def ensure_schema(script: str, *, conn: sqlite3.Connection | None = None, db_path: str | Path | None = None) -> None:
    """Make sure every object ``script`` creates exists, running ``script`` under the write lock when one is missing.

    With ``conn`` this uses that connection, which must not be inside a transaction; otherwise it uses a fresh
    connection to ``db_path``, or to the active database when that is None.
    """
    names = _object_names(script)
    own = conn is None
    if own:
        conn = get_connection(str(db_path) if db_path is not None else active_default_db_path())
    try:
        if not _all_present(conn, names):
            _set_up(conn, script)
    finally:
        if own:
            conn.close()
