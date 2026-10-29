"""A value written after a matching value the user stated later reads as replaced by that later statement.

Found 2026-10-27 in the review of the receipt-authority fix: once a receipt rebuilt late no longer replaces a value
stated after it, the late value ("26 km", stated 10 March, written after "now 31 km", stated 2 April) was linked to
nothing, so both read as current. Written in statement order, the same turns read "26 km [REPLACED later by "31 km"]".
Statement days decide the link whatever the write order: the earliest later record replaces the late value, and a
rebuilt receipt links once. A value already replaced before the late one is written keeps its link (it is not relinked).
"""
from __future__ import annotations

import json

import pytest

import core.memory_receipts as mr
from core.evidence_compiler import _render_line
from core.vool_memory import VoolMemory
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")

CHAT = "late-value"
JANUARY_20 = 1737370800.0
MARCH_10 = 1741597200.0
APRIL_2 = 1743584400.0
MAY_5 = 1746442800.0


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


def _receipts(mem):
    return {r["receipt_id"]: r for r in mr.receipts_for_scope(mem, CHAT, role="user")}


def _fact(receipts, receipt_id, value):
    return next(f for f in receipts[receipt_id]["facts"] if f.get("value") == value)


def _head_is_current(mem, receipt_id):
    row = mem._conn.execute("SELECT * FROM memory_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
    receipt = dict(row)
    for name in ("facts", "changes", "withdraws", "replaced_by"):
        receipt[name] = json.loads(receipt[name + "_json"])
    body = mem._conn.execute("SELECT body FROM source_occurrences WHERE occurrence_id = ?", (receipt["occurrence_id"],)).fetchone()[0]
    receipt["said"] = mr._strip_envelope(body)[0]
    return receipt["head_text"] == mr.head_text(receipt)


def _links(receipts):
    return sorted((c["old_value"], c["new_value"]) for r in receipts.values() for c in r.get("changes") or [])


def test_a_late_earlier_value_is_replaced_by_the_value_stated_after_it(mem):
    correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    receipts = _receipts(mem)

    assert _fact(receipts, earlier["receipt_id"], "26 km").get("replaced_by") == correction["receipt_id"]
    assert _fact(receipts, correction["receipt_id"], "31 km").get("replaced_by") is None
    assert _links(receipts) == [("26 km", "31 km")]
    assert [e["receipt_id"] for e in receipts[earlier["receipt_id"]]["replaced_by"]] == [correction["receipt_id"]]
    line = _render_line(receipts[earlier["receipt_id"]], _fact(receipts, earlier["receipt_id"], "26 km"), receipts)
    assert '[REPLACED later by "31 km" stated 2025-04-02; not current]' in line, line
    # the correction's head names its new change, so the forget sweep still reads it as a projection, not authored text
    assert _head_is_current(mem, correction["receipt_id"]) and _head_is_current(mem, earlier["receipt_id"])


def test_written_in_either_order_the_turns_read_the_same(mem, tmp_path, monkeypatch):
    _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    in_order = _receipts(mem)

    late = VoolMemory(runtime_home=str(_profile(tmp_path / "late")))
    try:
        _write(late, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
        _write(late, "My longest tunnel route so far is 26 km.", MARCH_10)
        written_late = _receipts(late)
    finally:
        late.close()

    def current(receipts):
        return sorted(f["value"] for r in receipts.values() for f in r["facts"] if f.get("value_type") == "measure"
                      and not f.get("replaced_by"))

    assert _links(in_order) == _links(written_late) == [("26 km", "31 km")]
    assert current(in_order) == current(written_late) == ["31 km"]


def test_a_value_replaced_before_the_late_one_was_written_keeps_its_link(mem):
    first = _write(mem, "My longest tunnel route so far is 20 km.", JANUARY_20)
    correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    receipts = _receipts(mem)

    assert _fact(receipts, first["receipt_id"], "20 km").get("replaced_by") == correction["receipt_id"]
    assert _fact(receipts, earlier["receipt_id"], "26 km").get("replaced_by") == correction["receipt_id"]
    assert _fact(receipts, correction["receipt_id"], "31 km").get("replaced_by") is None


def _rebuild(mem, receipt_id):
    mem._conn.execute("DELETE FROM memory_receipts WHERE receipt_id = ?", (receipt_id,))
    mem._conn.commit()
    assert mr.rebuild_missing_receipts(mem, CHAT) == 1


@pytest.mark.parametrize("late", [True, False], ids=["written-late", "written-in-order"])
def test_a_rebuilt_earlier_receipt_links_once(mem, late):
    if late:
        correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
        earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    else:
        earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
        correction = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    _rebuild(mem, earlier["receipt_id"])
    receipts = _receipts(mem)

    assert _links(receipts) == [("26 km", "31 km")]
    change = receipts[correction["receipt_id"]]["changes"][0]
    assert change["old_receipt"] in receipts and change["old_receipt"] != earlier["receipt_id"]
    assert _fact(receipts, change["old_receipt"], "26 km").get("replaced_by") == correction["receipt_id"]
    assert _head_is_current(mem, correction["receipt_id"])


def test_the_earliest_later_value_replaces_the_late_value(mem):
    first = _write(mem, "Correction, my longest tunnel route is now 31 km.", APRIL_2)
    second = _write(mem, "Correction again, my longest tunnel route is now 35 km.", MAY_5)
    earlier = _write(mem, "My longest tunnel route so far is 26 km.", MARCH_10)
    receipts = _receipts(mem)

    assert _fact(receipts, first["receipt_id"], "31 km").get("replaced_by") == second["receipt_id"]
    assert _fact(receipts, earlier["receipt_id"], "26 km").get("replaced_by") == first["receipt_id"]
    assert _links(receipts) == [("26 km", "31 km"), ("31 km", "35 km")]


@pytest.mark.parametrize("late", [True, False], ids=["written-late", "written-in-order"])
def test_two_equal_values_in_one_turn_are_each_replaced(mem, late):
    turns = [("My longest tunnel route so far is 26 km. My longest river swim so far is 26 km.", MARCH_10),
             ("Correction, my longest tunnel route is now 31 km. Correction, my longest river swim is now 31 km.", APRIL_2)]
    for body, statement_at in (reversed(turns) if late else turns):
        _write(mem, body, statement_at)
    receipts = _receipts(mem)

    assert _links(receipts) == [("26 km", "31 km"), ("26 km", "31 km")]
    assert all(f.get("replaced_by") for r in receipts.values() for f in r["facts"] if f.get("value") == "26 km")
