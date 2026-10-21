"""A forget and an attachment turn of another chat, at the same moment, both finish.

Measured 2026-10-21 on windows/port-forget-chat-documents (66b0c69): a forget holds the forget
ledger's lock for its whole command and then takes the attachment store's lock to change the chat's
documents, while serving or staging an attachment took the store's lock first and the ledger's lock
inside it. One of each at once in a daemon hung both threads for good, and every later upload,
attachment turn and forget behind them.

The gates below only order the two threads so each holds its first lock when the other asks for it.
"""
from __future__ import annotations

import random
import threading

import pytest

from core import chat_attachments as ca
from tests.test_forget_gaps_review_two import _attach, _canonical, _value
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend

_WAIT = 10.0
_JOIN = 30.0


def _forget_in_another_chat(tmp_path, rng):
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The gate code is {value}.")])
    _attach(chat, "turn-1", f"Garden notes.\nThe gate code is {value}.\n", "garden.txt")
    return profile, chat, value


def _gate_the_forget(monkeypatch, in_fence: threading.Event) -> None:
    real = ca.forget_in_documents

    def forget_in_documents(session_id, token):
        in_fence.set()  # the forget command holds the ledger's lock here
        return real(session_id, token)

    monkeypatch.setattr(ca, "forget_in_documents", forget_in_documents)


def _run_both(first, second, started: threading.Event) -> dict:
    results: dict = {}

    def run(key, fn):
        try:
            results[key] = fn()
        except BaseException as exc:  # reported below; a hang is the failure under test
            results[key] = exc

    one = threading.Thread(target=run, args=("first", first), daemon=True)
    two = threading.Thread(target=run, args=("second", second), daemon=True)
    one.start()
    assert started.wait(_WAIT), "the attachment turn never reached the store's lock"
    two.start()
    one.join(_JOIN)
    two.join(_JOIN)
    assert not one.is_alive() and not two.is_alive(), "the forget and the attachment turn deadlocked"
    return results


def test_a_forget_and_another_chats_attachment_serve_both_finish(tmp_path, monkeypatch):
    rng = random.Random(5521)
    profile, chat, value = _forget_in_another_chat(tmp_path, rng)
    other = _canonical(rng)
    doc = ca.stage_document(session_id=other, data=b"Plain words.\n", declared_name="plain.txt")
    ca.bind_to_turn(session_id=other, turn_id="turn-1", attachment_ids=[doc["id"]])

    in_lock, in_fence = threading.Event(), threading.Event()
    _gate_the_forget(monkeypatch, in_fence)
    real_bound = ca._bound_records

    def bound_records(session_id, turn_id):
        records = real_bound(session_id, turn_id)
        if session_id == other:
            in_lock.set()  # the serve holds the store's lock here
            in_fence.wait(_WAIT)
        return records

    monkeypatch.setattr(ca, "_bound_records", bound_records)
    results = _run_both(
        lambda: ca.model_attachments_for_turn(session_id=other, turn_id="turn-1"),
        lambda: _forget_command(profile, chat, f"Forget {value}"),
        in_lock,
    )
    served, (handled, reply) = results["first"], results["second"]
    assert [entry.get("text") for entry in served] == ["Plain words.\n"], served
    assert handled and reply.startswith("Forget applied"), reply
    assert not ca.documents_carrying(chat, value)


def test_a_forget_and_another_chats_upload_both_finish(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(5522)
    profile, chat, value = _forget_in_another_chat(tmp_path, rng)
    other = _canonical(rng)

    in_lock, in_fence = threading.Event(), threading.Event()
    _gate_the_forget(monkeypatch, in_fence)
    real_writing = revocation.writing

    def writing(payload, chat_id=None, *args, **kwargs):
        if chat_id == other and not in_lock.is_set():
            in_lock.set()  # the upload is about to take the ledger's writer fence
            in_fence.wait(_WAIT)
        return real_writing(payload, chat_id, *args, **kwargs)

    monkeypatch.setattr(revocation, "writing", writing)
    results = _run_both(
        lambda: ca.stage_document(session_id=other, data=b"Plain words.\n", declared_name="plain.txt"),
        lambda: _forget_command(profile, chat, f"Forget {value}"),
        in_lock,
    )
    staged, (handled, reply) = results["first"], results["second"]
    assert isinstance(staged, dict) and staged.get("id"), staged
    assert handled and reply.startswith("Forget applied"), reply
    assert not ca.documents_carrying(chat, value)
