"""Windows: at startup VOOL removes the receipt files a private chat's erase could not.

An honesty receipt file another program held open when VOOL closed outlives its chat's erase (and the
background retry, which ends with the process; tests/test_windows_073_private_trace_erase_in_use.py).
The owner's pick (17 Nov): on Windows, startup removes the honesty receipt files (.jsonl, .lock, .tmp)
of every chat still private with nothing saved, and nothing else: no other receipt ledger, no run
record, no database row. A chat that is saved,
no longer private, waiting on the person, has a request running, or whose state cannot be read keeps
its files. Off Windows startup is unchanged: nothing is read and nothing is removed.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from core import private_chat
from core import private_chat_traces as traces
from core import windows_write_gate as gate

CHATS = {name: "openclaw:" + digit * 20 for name, digit in
         (("private", "1"), ("saved", "2"), ("public", "3"), ("running", "4"), ("waiting", "5"))}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Five chats in the switch store, each with two receipt files and one run record."""
    db = tmp_path / "runtime.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE private_chat_sessions (session_id TEXT PRIMARY KEY, started_at TEXT)")
    conn.execute("CREATE TABLE runtime_sessions (session_id TEXT)")
    conn.execute("CREATE TABLE message_queue (session_id TEXT, status TEXT)")
    for sid in CHATS.values():
        conn.execute("INSERT INTO private_chat_sessions VALUES (?, '')", (sid,))
        conn.execute("INSERT INTO runtime_sessions VALUES (?)", (sid,))
    conn.execute("INSERT INTO message_queue VALUES (?, 'pending')", (CHATS["waiting"],))
    conn.commit()
    conn.close()
    files = {}
    for name, sid in CHATS.items():
        files[sid] = [tmp_path / f"{name}.jsonl", tmp_path / f"{name}.lock", tmp_path / f"{name}.jsonl.1.tmp"]
        for path in files[sid]:
            path.write_text("x", encoding="utf-8")
    other = {sid: [tmp_path / f"{name}.v2.jsonl", tmp_path / f"{name}.v2.head.json"] for name, sid in CHATS.items()}
    for paths in other.values():
        for path in paths:
            path.write_text("x", encoding="utf-8")
    opened = []

    def connect():
        opened.append(True)
        return sqlite3.connect(db)

    monkeypatch.setattr(traces, "get_connection", connect)
    monkeypatch.setattr(traces, "_honesty_ledger_files", lambda sid: list(files.get(sid, [])))
    monkeypatch.setattr(traces, "_ledger_files", lambda sid: list(files.get(sid, [])) + list(other.get(sid, [])))
    monkeypatch.setattr(traces, "chat_is_unsaved", lambda sid: sid != CHATS["saved"])
    monkeypatch.setattr(private_chat, "is_private", lambda sid: sid != CHATS["public"])
    monkeypatch.setattr(private_chat, "chat_has_request_in_flight", lambda sid, **_kw: sid == CHATS["running"])
    return {"db": db, "files": files, "other": other, "opened": opened}


def _left(home) -> dict[str, bool]:
    return {name: all(path.exists() for path in home["files"][sid]) for name, sid in CHATS.items()}


def test_on_windows_startup_removes_only_an_unsaved_private_chats_receipt_files(home, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)

    with caplog.at_level(logging.DEBUG, logger=traces.__name__):
        assert traces.sweep_receipt_files_at_startup() == 3

    assert _left(home) == {"private": False, "saved": True, "public": True, "running": True, "waiting": True}
    conn = sqlite3.connect(home["db"])
    try:
        rows = conn.execute("SELECT count(*) FROM runtime_sessions").fetchone()[0]
        switches = conn.execute("SELECT count(*) FROM private_chat_sessions").fetchone()[0]
    finally:
        conn.close()
    assert (rows, switches) == (5, 5), "no run record and no switch row is touched"
    assert all(path.exists() for paths in home["other"].values() for path in paths), "no other ledger is swept"
    assert caplog.records == []


def test_on_windows_a_chat_whose_state_cannot_be_read_keeps_its_files(home, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)

    def broken(_sid):
        raise OSError("store unreadable")

    monkeypatch.setattr(traces, "chat_is_unsaved", broken)
    assert traces.sweep_receipt_files_at_startup() == 0
    assert all(_left(home).values())


def test_on_windows_an_unreadable_switch_store_logs_no_chat(home, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)

    def fail():
        raise sqlite3.OperationalError(f"unable to open {CHATS['private']}")

    monkeypatch.setattr(traces, "get_connection", fail)
    with caplog.at_level(logging.WARNING, logger=traces.__name__):
        assert traces.sweep_receipt_files_at_startup() == 0
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "(OperationalError)" in logged and CHATS["private"] not in logged
    assert all(record.exc_info is None for record in caplog.records)


def test_on_windows_a_chat_whose_files_cannot_be_listed_is_skipped_and_startup_goes_on(home, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    real = traces._honesty_ledger_files

    def listing(sid):
        if sid == CHATS["private"]:
            raise ValueError(f"bad digest for {sid}")
        return real(sid)

    monkeypatch.setattr(traces, "_honesty_ledger_files", listing)
    monkeypatch.setattr(traces, "_still_erasable", lambda sid: True)
    with caplog.at_level(logging.WARNING, logger=traces.__name__):
        assert traces.sweep_receipt_files_at_startup() == 9  # saved, public and running (forced erasable here); waiting is held back by its wait
    assert _left(home)["private"] is True
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "skipped a chat (ValueError)" in logged and CHATS["private"] not in logged
    assert all(record.exc_info is None for record in caplog.records)


def test_off_windows_startup_reads_nothing_and_removes_nothing(home, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)

    assert traces.sweep_receipt_files_at_startup() == 0
    assert all(_left(home).values())
    assert home["opened"] == []


def test_the_server_runs_the_sweep_when_it_builds_its_app(monkeypatch):
    from apps import vool_api_server as server
    from core import persistent_memory
    from core.agent_team import service as agent_team_service
    from core.council import pin_lock

    calls = []
    monkeypatch.setattr(traces, "sweep_receipt_files_at_startup", lambda: calls.append("sweep") or 0)
    monkeypatch.setattr(pin_lock, "reset_on_startup", lambda: None)
    monkeypatch.setattr(agent_team_service, "recover_all", lambda: None)
    monkeypatch.setattr(persistent_memory, "_recover_admissions", lambda: None)
    monkeypatch.setattr(server, "create_api_app", lambda **_kw: "app")
    assert server.create_app(runtime=object()) == "app"
    assert calls == ["sweep"]
