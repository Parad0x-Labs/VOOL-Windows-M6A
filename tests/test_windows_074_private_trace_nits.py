"""Windows: a private chat's erase spends little time on a held receipt file, and the background
retry checks the chat again right before each delete.

0.7.3 (tests/test_windows_073_private_trace_erase_in_use.py) retried a held file 30 times on the
request path, about 3 seconds per file, before leaving it to the background retry. The background
pass checked the chat once per pass, so a save landing between that check and a later file's delete
could still lose that file. Now the request path tries a held file at most 5 times, and the
background retry checks the chat before every delete. Off Windows nothing changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import private_chat
from core import private_chat_traces as traces
from core import windows_write_gate as gate

SID = "openclaw:" + "e7" * 10


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    files = [tmp_path / name for name in ("a.jsonl", "a.lock", "a.head.json")]
    for path in files:
        path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(traces, "_ledger_files", lambda _sid: list(files))
    monkeypatch.setattr(traces, "get_connection", lambda: sqlite3.connect(":memory:", isolation_level=None))
    monkeypatch.setattr(traces, "_IN_USE_PAUSE_S", 0.0)
    monkeypatch.setattr(traces, "_LATER_PAUSES_S", (0.0,) * 3)
    monkeypatch.setattr(private_chat, "is_private", lambda _sid: True)
    monkeypatch.setattr(private_chat, "chat_has_request_in_flight", lambda _sid, **_kw: False)
    monkeypatch.setattr(traces, "chat_is_unsaved", lambda _sid: True)
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    return files


def _count_unlinks(monkeypatch, held: dict[str, int]) -> dict[str, int]:
    """Each named file fails its next N deletes as an open handle does; every attempt is counted."""
    real = Path.unlink
    tries: dict[str, int] = {}

    def unlink(self, *args, **kwargs):
        tries[self.name] = tries.get(self.name, 0) + 1
        if held.get(self.name, 0):
            held[self.name] -= 1
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return tries


def test_on_windows_the_request_path_tries_a_held_file_at_most_five_times(ledgers, monkeypatch):
    tries = _count_unlinks(monkeypatch, {ledgers[0].name: 10_000})
    started = []
    monkeypatch.setattr(traces, "_erase_later", lambda sid, paths: started.append(list(paths)))

    result = traces.erase_chat_traces(SID)

    assert tries[ledgers[0].name] <= 5
    assert result["removed"]["receipt_files_in_use"] == 1
    assert started == [[ledgers[0]]]


def test_on_windows_the_background_retry_checks_the_chat_before_each_delete(ledgers, monkeypatch):
    # Both of the first two files are held through the request path; the chat is saved between the
    # background pass's delete of the first and its delete of the second.
    tries = _count_unlinks(monkeypatch, {ledgers[0].name: traces._IN_USE_TRIES, ledgers[1].name: traces._IN_USE_TRIES})
    saved = {"now": False}
    real_check = traces._still_erasable

    def check(sid):
        if tries.get(ledgers[0].name, 0) > traces._IN_USE_TRIES:
            saved["now"] = True  # the first background delete has happened: the person saves now
        return real_check(sid)

    monkeypatch.setattr(traces, "_still_erasable", check)
    monkeypatch.setattr(traces, "chat_is_unsaved", lambda _sid: not saved["now"])
    workers = []
    real_later = traces._erase_later
    monkeypatch.setattr(traces, "_erase_later", lambda sid, paths: workers.append(real_later(sid, paths)))

    traces.erase_chat_traces(SID)
    workers[0].join(10)

    assert not ledgers[0].exists()
    assert ledgers[1].exists(), "a file of a chat saved mid-pass must stay"


def test_off_windows_a_held_file_is_tried_once_and_raises_as_before(ledgers, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    tries = _count_unlinks(monkeypatch, {ledgers[0].name: 1})

    with pytest.raises(PermissionError):
        traces.erase_chat_traces(SID)
    assert tries == {ledgers[0].name: 1}
