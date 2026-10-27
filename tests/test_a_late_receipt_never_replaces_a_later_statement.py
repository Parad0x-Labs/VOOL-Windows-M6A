"""A receipt written late never replaces a value the user stated later.

Measured 2026-10-27 while root-causing CI run 40441789238 (tests (0)): when the first turn's receipt write fails and
compile_packet's rebuild_missing_receipts writes it after the correction's receipt already exists, the earlier turn
("my longest tunnel route so far is 26 km", 10 March) was linked as replacing the correction ("now 31 km", 2 April).
The packet then read "31 km [REPLACED later by "26 km" stated 2025-03-10]". The state branch of find_changes already
lets statement days decide, not write order; the typed-value branch (measure, duration, amount, count, age) did not.
"""
from __future__ import annotations

import pytest

import core.memory_receipts as mr
from core.vool_memory import VoolMemory
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")

CHAT = "late-receipt"
MARCH_10 = 1741597200.0
APRIL_2 = 1743584400.0


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_MEMORY_RECEIPTS", "1")
    memory = VoolMemory(runtime_home=str(_profile(tmp_path)))
    yield memory
    memory.close()


def _write(mem, body, statement_at):
    occurrence = mem.occurrence_store(chat_scope=CHAT, role="user", authority="observed-user-statement", body=body,
                                      statement_at=statement_at)
    written = mr.write_receipt(mem, occurrence, chat_scope=CHAT)
    assert written.status == "written", written
    return written.receipt


def _facts(mem, value):
    return [f for r in mr.receipts_for_scope(mem, CHAT, role="user") for f in r["facts"] if f.get("value") == value]


def test_an_earlier_statement_written_after_its_correction_replaces_nothing(mem):
    correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)

    assert earlier["changes"] == [], earlier["changes"]
    assert [f.get("replaced_by") for f in _facts(mem, "31 km")] == [None]
    assert correction["changes"] == []


def test_the_same_turns_in_statement_order_still_link_the_correction(mem):
    earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)

    assert [(c["old_value"], c["new_value"]) for c in correction["changes"]] == [("26 km", "31 km")]
    assert [f.get("replaced_by") for f in _facts(mem, "26 km")] == [correction["receipt_id"]]
    assert earlier["changes"] == []
