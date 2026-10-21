"""A chat's memory erase (chat delete, project delete, per-row forget, finalization sweep) and another
chat's "Forget X", at the same moment, both finish.

Measured 2026-10-21 on windows/port-forget-chat-documents (aaa4a2d) and port head 4b08659: a forget
holds the forget fence (`revocation.serialized`) for its whole command and takes
`entries._MEMORY_ENTRY_LOCK` inside it, while these four erasers took `_MEMORY_ENTRY_LOCK` first and
reached the fence inside it through `rewrite_jsonl`. One of each at once hung both threads for good,
and every later memory writer, forget and attachment turn behind them.

The gates only order the two threads so each holds its first lock when the other asks for it. Each
case runs on its own copies of the two process locks, so a case that deadlocks leaves its threads
stuck on those copies and the next case still runs.
"""
from __future__ import annotations

import random
import threading

import pytest

import core.memory.entries as entries
import core.memory.revocation as revocation
from core.context_namespace import ensure_chat_namespace
from core.persistent_memory import append_conversation_event
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend

_WAIT = 10.0
_JOIN = 30.0
_REQUEST = "req-e1-lock-order"
_ERASERS = ("delete_conversation_session", "forget_sessions_memory", "forget_memory_record", "sweep_governed_memory_rows", "correction")


def _canonical(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _value(rng: random.Random) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5)) + f"-{rng.randint(1000, 9999)}"


def _remember(monkeypatch, chat: str, fact: str) -> str:
    ensure_chat_namespace(chat, grant_current_receipts=False)
    policy = entries.resolve_memory_access_policy(chat_id=chat)
    append_conversation_event(session_id=chat, user_input="hi", assistant_output="hey", access_policy=policy)
    with monkeypatch.context() as m:
        m.setattr(entries, "_current_request_lineage", lambda: _REQUEST)
        assert entries.record_memory_entry(
            fact, category="fact", session_id=chat, source="test", confidence=0.9,
            keywords=fact.lower().split()[:8], share_scope="local_only", scope="chat",
            authority="confirmed_memory", fact_key=None, expires_at=None, review_after=None,
            access_policy=policy,
        )
    rows = [r for r in entries._load_jsonl(entries.memory_entries_path()) if r.get("session_id") == chat and r.get("record_id")]
    assert len(rows) == 1 and rows[0].get("request_id") == _REQUEST, rows
    return str(rows[0]["record_id"])


@pytest.mark.parametrize("eraser", _ERASERS)
def test_a_memory_erase_and_another_chats_forget_both_finish(tmp_path, monkeypatch, eraser):
    # The case's own process locks: a deadlock here never reaches the next case.
    monkeypatch.setattr(entries, "_MEMORY_ENTRY_LOCK", threading.Lock())
    monkeypatch.setattr(revocation, "_LOCK", threading.RLock())

    rng = random.Random(7101 + _ERASERS.index(eraser))
    profile = _profile(tmp_path)
    chat, other = _canonical(rng), _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The gate code is {value}.")])
    kept = _value(rng)
    record_id = _remember(monkeypatch, other, f"The locker code is {kept}.")

    in_entry_lock, in_fence = threading.Event(), threading.Event()
    real_rewrite, real_forget = entries.rewrite_jsonl, entries.forget_structured_memory

    def rewrite_jsonl(path, rows):
        if threading.current_thread().name == "eraser" and path == entries.memory_entries_path() and not in_entry_lock.is_set():
            in_entry_lock.set()  # the eraser holds _MEMORY_ENTRY_LOCK and is about to enter the fence
            if not revocation._LOCK._is_owned():
                # Let the forget take the fence first. An eraser already holding the fence has nothing to wait for.
                in_fence.wait(_WAIT)
        return real_rewrite(path, rows)

    def forget_structured_memory(*args, **kwargs):
        if threading.current_thread().name != "eraser":
            in_fence.set()  # the forget holds the fence and asks for _MEMORY_ENTRY_LOCK next
        return real_forget(*args, **kwargs)

    monkeypatch.setattr(entries, "rewrite_jsonl", rewrite_jsonl)
    monkeypatch.setattr(entries, "forget_structured_memory", forget_structured_memory)
    erase = {
        "delete_conversation_session": lambda: entries.delete_conversation_session(other),
        "forget_sessions_memory": lambda: entries.forget_sessions_memory([other]),
        "forget_memory_record": lambda: entries.forget_memory_record(record_id),
        "sweep_governed_memory_rows": lambda: entries.sweep_governed_memory_rows(_REQUEST),
        # "Correction: ..." erases the old value through forget_memory with no command fence
        # (persistent_memory.maybe_handle_memory_command: _CORRECTION_RE is not _FORGET_RE).
        "correction": lambda: entries.forget_memory(kept, chat_id=other),
    }[eraser]

    results: dict = {}

    def run(key, fn):
        try:
            results[key] = fn()
        except BaseException as exc:  # reported below; a hang is the failure under test
            results[key] = exc

    one = threading.Thread(target=run, args=("erase", erase), name="eraser", daemon=True)
    two = threading.Thread(target=run, args=("forget", lambda: _forget_command(profile, chat, f"Forget {value}")), daemon=True)
    one.start()
    assert in_entry_lock.wait(_WAIT), "the erase never reached the fence"
    two.start()
    one.join(_JOIN)
    two.join(_JOIN)
    assert not one.is_alive() and not two.is_alive(), "the memory erase and the forget deadlocked"

    assert results["erase"] and not isinstance(results["erase"], BaseException), results
    handled, reply = results["forget"]
    assert handled and reply.startswith("Forget applied"), reply
    assert not [r for r in entries._load_jsonl(entries.memory_entries_path())
                if r.get("record_id") == record_id and r.get("status") == "active"]
