"""A failure that drops execution or memory evidence leaves a warning naming where it happened.

Four best-effort reads and writes swallow every exception and fail closed:
``revocation.authorize_payload`` (a source row then reads as missing), ``execution_truth.record_execution``
(the turn's execution fact is not written), ``execution_truth.facts_for_turn`` (the turn reads as having
run nothing) and ``finalization.writer_may_persist_text`` (a derivative write is vetoed). Intermittent CI
reds on 22 Oct 2026 lost a retrieval row and an execution fact through these with nothing logged, so
nothing said which one failed or why.

Each must still fail closed exactly as before, and must now log a warning that names the function and
the exception type -- and never the exception's text, which can carry the very words being withheld.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

SECRET = "kiln locker 7428"


def _warned(caplog, function: str, exception: str) -> list[str]:
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert not any(SECRET in message for message in messages), messages
    return [message for message in messages if function in message and exception in message]


def test_an_unreadable_forget_authority_still_withholds_and_is_logged(monkeypatch, caplog):
    from core.memory import revocation

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError(f"database is locked while reading {SECRET}")

    monkeypatch.setattr(revocation, "revoked_tokens", unreadable)
    with caplog.at_level(logging.WARNING):
        assert revocation.authorize_payload("The " + SECRET + " is on the shelf.", "chat-a") is False
    assert _warned(caplog, "authorize_payload", "OperationalError"), caplog.text


def test_an_execution_fact_that_cannot_be_written_is_logged(monkeypatch, caplog):
    from core import execution_truth

    def no_connection():
        raise sqlite3.OperationalError(f"database is locked for {SECRET}")

    monkeypatch.setattr(execution_truth, "_conn", no_connection)
    with caplog.at_level(logging.WARNING):
        fact = execution_truth.record_execution(session_id="chat-a", turn_key="turn-1", kind="tool",
                                                name="workspace.write_file", ok=True, detail={"note": SECRET})
    assert fact == ""
    assert _warned(caplog, "record_execution", "OperationalError"), caplog.text


def test_execution_facts_that_cannot_be_read_are_logged(monkeypatch, caplog):
    from core import execution_truth

    def no_connection():
        raise sqlite3.OperationalError(f"disk I/O error near {SECRET}")

    monkeypatch.setattr(execution_truth, "_conn", no_connection)
    with caplog.at_level(logging.WARNING):
        assert execution_truth.facts_for_turn("turn-1", session_id="chat-a") == []
    assert _warned(caplog, "facts_for_turn", "OperationalError"), caplog.text


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, RuntimeError])
def test_a_governance_store_failure_still_vetoes_the_write_and_is_logged(monkeypatch, caplog, failure):
    from core import finalization

    def unavailable(text):
        raise failure(f"availability store failed on {SECRET}")

    monkeypatch.setattr(finalization, "verdict_or_unavailable", unavailable)
    with caplog.at_level(logging.WARNING):
        assert finalization.writer_may_persist_text("The " + SECRET + " is on the shelf.") is False
    assert _warned(caplog, "writer_may_persist_text", failure.__name__), caplog.text


def test_the_paths_that_succeed_log_nothing(monkeypatch, caplog):
    from core import finalization
    from core.memory import revocation

    monkeypatch.setattr(revocation, "revoked_tokens", lambda *args, **kwargs: [])
    monkeypatch.setattr(finalization, "verdict_or_unavailable", lambda text: "available")
    with caplog.at_level(logging.WARNING):
        assert revocation.authorize_payload("nothing forgotten here", "chat-a") is True
        assert finalization.writer_may_persist_text("nothing withheld here") is True
    assert not [record for record in caplog.records
                if any(name in record.getMessage() for name in ("authorize_payload", "writer_may_persist_text"))]
