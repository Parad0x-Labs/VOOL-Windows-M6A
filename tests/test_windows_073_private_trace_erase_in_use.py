"""Windows: a private chat's erase removes every receipt file, even while a reader has one open.

Windows run 43964307548 (58a267fb): tests/test_private_chat_leaves_no_trace_v072.py found a still-private
chat's honesty receipt ledger, its lock, its memory-packet receipts and a line in ``vool_api.log`` after
its turns ended. At the end of each private request ``core.private_chat_traces.erase_chat_traces`` deletes
the chat's run rows and then unlinks its receipt files. On Windows a file that another handle has open (a
reader, an indexer, antivirus; in that test, its own home scanner) cannot be deleted: the unlink fails with
a sharing violation. Only FileNotFoundError was expected, so the PermissionError stopped the loop with the
later files still there, and ``erase_if_unsaved`` logged the traceback, whose paths are named with the
chat's digest, into ``vool_api.log``.

On Windows the erase now retries a file in use, goes on to the rest when one stays in use, and logs
neither a path nor the chat. Off Windows nothing changes.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from core import private_chat_traces as traces
from core import windows_write_gate as gate

SID = "openclaw:" + "d4" * 10


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    """Three receipt files of the chat, and a run store with nothing in it."""
    names = ["c495697830c206b3.jsonl", "c495697830c206b3.lock", "c495697830c206b3.head.json"]
    files = [tmp_path / name for name in names]
    for path in files:
        path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(traces, "_ledger_files", lambda _sid: list(files))
    monkeypatch.setattr(traces, "get_connection", lambda: sqlite3.connect(":memory:", isolation_level=None))
    monkeypatch.setattr(traces, "_IN_USE_PAUSE_S", 0.0)
    return files


def _held(monkeypatch, busy: dict[str, int]) -> None:
    """A file named in `busy` fails its next N deletes with a sharing violation, as an open handle does."""
    real = Path.unlink

    def unlink(self, *args, **kwargs):
        left = busy.get(self.name, 0)
        if left:
            busy[self.name] = left - 1
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)


def test_on_windows_a_file_briefly_in_use_is_still_removed(ledgers, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _held(monkeypatch, {ledgers[0].name: 3})

    result = traces.erase_chat_traces(SID)

    assert [path.exists() for path in ledgers] == [False, False, False]
    assert result["removed"]["receipt_files"] == 3


def test_on_windows_one_file_held_open_does_not_keep_the_others_and_logs_no_name(ledgers, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _held(monkeypatch, {ledgers[0].name: 10_000})

    with caplog.at_level(logging.INFO, logger=traces.__name__):
        result = traces.erase_chat_traces(SID)

    assert [path.exists() for path in ledgers] == [True, False, False]
    assert result["removed"]["receipt_files"] == 2
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "still in use" in logged
    assert "c495697830c206b3" not in logged and SID not in logged and "d4d4" not in logged


def test_on_windows_a_failed_erase_logs_no_traceback(monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(traces, "chat_is_unsaved", lambda _sid: True)

    def fail(_sid):
        raise OSError(f"cannot remove C:\\home\\data\\honesty_receipts\\c495697830c206b3.jsonl for {SID}")

    monkeypatch.setattr(traces, "erase_chat_traces", fail)
    with caplog.at_level(logging.WARNING, logger=traces.__name__):
        assert traces.erase_if_unsaved(SID) is None

    assert caplog.records and all(record.exc_info is None for record in caplog.records)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "could not be erased (OSError)" in logged
    assert "c495697830c206b3" not in logged and SID not in logged


def test_off_windows_the_erase_is_as_before(ledgers, monkeypatch, caplog):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    _held(monkeypatch, {ledgers[0].name: 1})

    with pytest.raises(PermissionError):
        traces.erase_chat_traces(SID)
    assert [path.exists() for path in ledgers] == [True, True, True]

    monkeypatch.setattr(traces, "chat_is_unsaved", lambda _sid: True)
    _held(monkeypatch, {ledgers[0].name: 1})
    with caplog.at_level(logging.WARNING, logger=traces.__name__):
        assert traces.erase_if_unsaved(SID) is None
    assert [record.exc_info is not None for record in caplog.records] == [True]
