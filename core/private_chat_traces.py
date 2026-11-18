"""Private chat: a chat that was private from its first message leaves no record that it existed.

A chat whose first message turned private chat on (core.private_chat) is given no namespace row
(core.context_namespace), so it is in no chat list. But a turn of it still ran through VOOL's run
machinery, which keys its working records by the chat's id: the runtime session, its events and
attempts, the task row, execution facts, the context-generation counter, the provenance and
provider-call manifests, the semantic admission ids, the per-chat receipt ledgers. Each holds ids,
states and digests, never a word of the chat, but together they say "this chat existed, at these
times, with this many turns". Some of them the turn needs while it runs; none is needed after it.

This module is the ONE owner of that cleanup. At the end of every private request of a chat that
has nothing saved (:func:`erase_if_unsaved`) it deletes those rows (the runtime database zeroes
deleted content: ``PRAGMA secure_delete``), checkpoints the WAL so no frame keeps them, and removes
the chat's receipt ledger files. :func:`sweep_unsaved_private_chats` does the same for every chat
still private, for records a turn cut off by a stop (or an older build) left behind; it is not run
at startup, because it erases records that existed before this build, which waits on the owner's
word. On Windows only, startup does remove one thing (:func:`sweep_receipt_files_at_startup`, the
owner's picks): the receipt ledger files (the honesty receipt ledger, its lock and temporary files,
and the chat's own receipts_v2 ledgers) of every chat still private with nothing saved, which a file
held open when VOOL closed can leave behind. No run record or database row is touched. The private switch itself (``private_chat_sessions``) stays while
the chat is private, by design.

Money is never erased. When the chat has a spend record (a money liability or a model spend
reservation), every money table keeps its rows, and so do the chat's task rows and provider-call
manifests, which are what such a receipt is traced back through (the MONEY RULE: every spend keeps
its receipt).

A chat that was saved before it went private keeps its records: it exists, and its id is in its
own chat list row. Nothing here touches such a chat.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from storage.db import get_connection

__all__ = [
    "chat_is_unsaved",
    "erase_chat_traces",
    "erase_if_unsaved",
    "sweep_receipt_files_at_startup",
    "sweep_unsaved_private_chats",
]

_log = logging.getLogger(__name__)

#: (table, column, how the column names the chat). "eq": the column is the chat id; "in": the
#: chat id is part of the column's text (a composed id, or a JSON document).
_RUN_RECORDS: tuple[tuple[str, str, str], ...] = (
    ("runtime_session_events", "session_id", "eq"),
    ("runtime_sessions", "session_id", "eq"),
    ("runtime_attempts", "session_id", "eq"),
    ("runtime_checkpoints", "session_id", "eq"),
    ("notification_items", "session_id", "eq"),
    ("session_hive_watch_state", "session_id", "eq"),
    ("execution_facts", "session_id", "eq"),
    ("turn_context_generations", "session_id", "eq"),
    ("semantic_admissions", "sr_id", "in"),
    ("message_queue", "session_id", "eq"),
    ("runtime_tool_receipts", "session_id", "eq"),
    ("runtime_followup_resolutions", "session_id", "eq"),
    ("operator_action_requests", "session_id", "eq"),
    ("fault_records", "session_id", "eq"),
    ("dialogue_turns", "session_id", "eq"),
    ("dialogue_sessions", "session_id", "eq"),
)
#: The chat is waiting on the person (an approval, a queued message, a turn to resume): its records
#: are what that wait runs on, so nothing is erased until it is over.
_WAITING: tuple[tuple[str, str], ...] = (
    ("runtime_checkpoints", "status IN ('running', 'interrupted', 'pending_approval')"),
    ("message_queue", "status IN ('pending', 'in_flight')"),
    ("operator_action_requests", "status IN ('pending', 'pending_approval')"),
)
#: Kept when the chat has a money record: a model spend reservation is traced back to its task.
_MONEY_LINKED: tuple[tuple[str, str, str], ...] = (
    ("local_tasks", "session_id", "eq"),
)


def _clean(session_id: Any) -> str:
    return str(session_id or "").strip()


def chat_is_unsaved(session_id: str) -> bool:
    """True when nothing of the chat is saved: no namespace row, no transcript row, no sidebar
    entry. Any fault answers False, so a chat that may exist is never erased."""
    sid = _clean(session_id)
    if not sid:
        return False
    try:
        from core.context_namespace import _load_saved_namespace
        from core.memory.entries import _load_session_meta_raw, recent_conversation_events

        if _load_saved_namespace(sid) is not None:
            return False
        if sid in _load_session_meta_raw():
            return False
        return not recent_conversation_events(sid, limit=1, include_artifacts=True)
    except Exception:
        _log.warning("could not tell whether a private chat has saved records; leaving them", exc_info=True)
        return False


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _waiting(conn: sqlite3.Connection, sid: str, tables: set[str]) -> bool:
    for table, condition in _WAITING:
        if table in tables and conn.execute(
            f'SELECT 1 FROM "{table}" WHERE session_id = ? AND {condition} LIMIT 1', (sid,)
        ).fetchone():
            return True
    return False


def _has_money(conn: sqlite3.Connection, sid: str, tables: set[str]) -> bool:
    tasks = "(SELECT task_id FROM local_tasks WHERE session_id = ?)" if "local_tasks" in tables else "(SELECT NULL)"
    if "effect_budget_money_liabilities" in tables and conn.execute(
        "SELECT 1 FROM effect_budget_money_liabilities WHERE session_id = ? "
        f"OR (task_id != '' AND task_id IN {tasks}) LIMIT 1",
        (sid, sid) if "local_tasks" in tables else (sid,),
    ).fetchone():
        return True
    return {"model_spend_reservations", "local_tasks"} <= tables and conn.execute(
        "SELECT 1 FROM model_spend_reservations WHERE task_id IN "
        "(SELECT task_id FROM local_tasks WHERE session_id = ?) LIMIT 1", (sid,)
    ).fetchone() is not None


def _delete(conn: sqlite3.Connection, table: str, column: str, how: str, sid: str) -> int:
    where = f'"{column}" = ?' if how == "eq" else f'instr(CAST("{column}" AS TEXT), ?) > 0'
    return conn.execute(f'DELETE FROM "{table}" WHERE {where}', (sid,)).rowcount or 0


def _honesty_ledger_files(sid: str) -> list[Path]:
    """The chat's honesty receipt ledger, its lock and its temporary files."""
    with contextlib.suppress(Exception):
        from core.honesty_receipt import _ledger_path

        ledger = _ledger_path(sid)
        return [ledger, ledger.with_suffix(".lock"), *ledger.parent.glob(ledger.name + ".*.tmp")]
    return []


def _ledger_files(sid: str) -> list[Path]:
    """The chat's own receipt ledger files: each is named by a digest of the chat id."""
    files: list[Path] = list(_honesty_ledger_files(sid))
    with contextlib.suppress(Exception):
        from core.evidence_kernel.receipts import _ledger_dir, ledger_path

        root = _ledger_dir()
        if root.is_dir():
            for kind in [path.name for path in root.iterdir() if path.is_dir()]:
                ledger = ledger_path(kind, sid)
                files += [ledger, ledger.with_suffix(".head.json"), ledger.with_suffix(".tmp")]
    return files


def _checkpoint(conn: sqlite3.Connection, *, attempts: int = 20) -> bool:
    """Copy the WAL into the database and empty it, so no WAL frame keeps a deleted row. A reader
    holding a snapshot makes it busy; it is retried briefly, then left to the next erase."""
    for _ in range(attempts):
        try:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.DatabaseError:
            return False
        if not busy or not busy[0]:
            return True
        time.sleep(0.1)
    _log.info("private chat erase: the WAL is still in use; it is emptied at the next erase")
    return False


def erase_chat_traces(session_id: str) -> dict[str, Any]:
    """Delete every run record that names the chat and remove its receipt ledgers.

    Callers decide that the chat is unsaved and private (:func:`erase_if_unsaved`). Returns what
    was removed and what was kept for money. Raises on a store fault.
    """
    sid = _clean(session_id)
    if not sid:
        raise ValueError("a chat id is required")
    removed: dict[str, int] = {}
    conn = get_connection()
    try:
        # One write transaction: the checks below and the deletes see the same rows, so a spend
        # or a saved row landing in between cannot be erased with the rest.
        conn.execute("BEGIN IMMEDIATE")
        tables = _tables(conn)
        if conn.execute("SELECT 1 FROM context_namespaces WHERE chat_id = ? LIMIT 1", (sid,)).fetchone() if (
            "context_namespaces" in tables
        ) else None:
            conn.rollback()
            return {"removed": {}, "kept_for_money": [], "skipped": "saved"}
        if _waiting(conn, sid, tables):
            conn.rollback()
            return {"removed": {}, "kept_for_money": [], "skipped": "waiting"}
        money = _has_money(conn, sid, tables)
        wanted = _RUN_RECORDS + (() if money else _MONEY_LINKED)
        for table, column, how in wanted:
            if table not in tables:
                continue
            try:
                count = _delete(conn, table, column, how, sid)
            except sqlite3.IntegrityError:
                # A row something else still points at stays; it is reported, never forced.
                _log.warning("private chat record in %s is still referenced; kept", table)
                continue
            if count:
                removed[table] = count
        conn.commit()
        _checkpoint(conn)
    finally:
        conn.close()
    files = 0
    left: list[Path] = []
    windows = _windows_host()
    for path in _ledger_files(sid):
        if windows:
            outcome = _unlink_on_windows(path)
            files += outcome == "removed"
            if outcome in ("in_use", "failed"):
                left.append(path)
            continue
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            files += 1
    if files:
        removed["receipt_files"] = files
    if left:
        # A caller never takes the erase for a clean one while files remain.
        removed["receipt_files_in_use"] = len(left)
        # Nothing is logged: a line would itself say a private chat was erased, and the file names
        # are the chat's digest. The files are removed in the background once their holder lets go.
        _erase_later(sid, left)
    return {"removed": removed, "kept_for_money": [t for t, _c, _h in _MONEY_LINKED] if money else []}


def _windows_host() -> bool:
    with contextlib.suppress(Exception):
        from core import windows_write_gate

        return windows_write_gate.active()
    return False


#: Windows only: a file another handle has open (a reader, an indexer, antivirus) cannot be deleted
#: until that handle closes, and the delete fails with a sharing violation instead of waiting.
_IN_USE_TRIES = 5
_IN_USE_PAUSE_S = 0.1
#: Windows only: when a file is still held after the retries, it is tried again in the background at
#: these pauses (about eight minutes in all); the chat's next private turn erases it again as well.
_LATER_PAUSES_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 120.0, 240.0)


def _unlink_on_windows(path: Path) -> str:
    """Remove one receipt file on Windows: "removed", "absent", "in_use" (still held after the
    retries) or "failed" (another error). One file left no longer stops the erase of the others."""
    for attempt in range(_IN_USE_TRIES):
        try:
            path.unlink()
            return "removed"
        except FileNotFoundError:
            return "absent"
        except PermissionError:
            if attempt + 1 < _IN_USE_TRIES:
                time.sleep(_IN_USE_PAUSE_S)
        except OSError:
            return "failed"
    return "in_use"


def _still_erasable(sid: str) -> bool | None:
    """For a background pass: True to erase now, None to wait (a request of the chat is running and
    may be writing its ledgers, .lock included), False to stop for good (the chat was saved or is no
    longer private: its files, under the same digest names, are its records now). Unknown is False."""
    try:
        from core.private_chat import chat_has_request_in_flight, is_private

        if not (is_private(sid) and chat_is_unsaved(sid)):
            return False
        return None if chat_has_request_in_flight(sid) else True
    except Exception:
        return False


def _erase_later(sid: str, paths: list[Path]) -> threading.Thread:
    """Windows only: keep trying to remove the files left behind, off the request path, while the
    chat is still private with nothing saved."""

    def run() -> None:
        pending = list(paths)
        for pause in _LATER_PAUSES_S:
            if not pending:
                return
            time.sleep(pause)
            state = _still_erasable(sid)
            if state is False:
                return
            if state is None:
                continue
            still: list[Path] = []
            for index, path in enumerate(pending):
                # Checked again before each delete: a save landing mid-pass keeps the rest, and a
                # request starting mid-pass puts the rest off to the next pass.
                if index:
                    state = _still_erasable(sid)
                    if state is False:
                        return
                    if state is None:
                        still.extend(pending[index:])
                        break
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    still.append(path)
            pending = still

    worker = threading.Thread(target=run, name="private-chat-erase", daemon=True)
    worker.start()
    return worker


def erase_if_unsaved(session_id: str) -> dict[str, Any] | None:
    """Erase the chat's run records when nothing of it is saved. Never raises."""
    sid = _clean(session_id)
    if not sid or not chat_is_unsaved(sid):
        return None
    try:
        return erase_chat_traces(sid)
    except Exception as exc:
        if _windows_host():
            # The traceback names the chat's files (their names are the chat's digest): the log
            # keeps the fault's kind only.
            _log.warning("a private chat's run records could not be erased (%s)", type(exc).__name__)
        else:
            _log.warning("a private chat's run records could not be erased", exc_info=True)
        return None


def sweep_unsaved_private_chats() -> int:
    """At startup: a private chat with nothing saved whose last turn did not reach its end (VOOL
    stopped mid-turn) still has that turn's records. Erase them. Returns how many chats."""
    try:
        conn = get_connection()
        try:
            if "private_chat_sessions" not in _tables(conn):
                return 0
            chats = [str(row[0]) for row in conn.execute("SELECT session_id FROM private_chat_sessions")]
        finally:
            conn.close()
    except Exception:
        _log.warning("private chat sweep could not read the switch store", exc_info=True)
        return 0
    return sum(1 for sid in chats if erase_if_unsaved(sid) is not None)


def sweep_receipt_files_at_startup() -> int:
    """Windows only, at startup: remove the receipt ledger files of every chat still private with
    nothing saved: the honesty receipt files (.jsonl, .lock, .tmp) and the chat's own receipts_v2
    ledgers (.jsonl, .head.json, .tmp), the same files its erase removes. A file another program held open when VOOL closed outlives the erase of its chat
    (and the background retry, which ends with the process). Only those files are removed: no run
    record, no database row. A chat whose state cannot be read, that has a request running, or that is
    waiting on the person, is left alone. Nothing is logged that names a chat or a file. Returns how many files were removed.
    Off Windows it does nothing."""
    if not _windows_host():
        return 0
    try:
        conn = get_connection()
        try:
            if "private_chat_sessions" not in _tables(conn):
                return 0
            tables = _tables(conn)
            chats = [str(row[0]) for row in conn.execute("SELECT session_id FROM private_chat_sessions")]
            # A chat waiting on the person (an approval, a queued message, a turn to resume) keeps
            # what that wait runs on, as its erase does.
            chats = [sid for sid in chats if not _waiting(conn, sid, tables)]
        finally:
            conn.close()
    except Exception as exc:
        _log.warning("private chat receipt sweep could not read the switch store (%s)", type(exc).__name__)
        return 0
    removed = 0
    for sid in chats:
        try:
            if _still_erasable(sid) is not True:
                continue
            for path in _ledger_files(sid):
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
        except Exception as exc:
            # That chat keeps its files; startup goes on. The fault's kind only: no chat, no path.
            _log.warning("private chat receipt sweep skipped a chat (%s)", type(exc).__name__)
    return removed
