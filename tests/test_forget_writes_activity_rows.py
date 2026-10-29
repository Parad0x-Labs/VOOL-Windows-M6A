"""A forget that changes a chat document leaves an Activity row for the change.

Mac writes the rows through `_forget_activity` (f066104 "...log forget writes", reshaped by vool-v08-memory
2a1f0e8 and 76f5eb93; ported as at 55c92e6): ``attachment_redacted`` when the forgotten words are removed
from a document or from its name, ``attachment_erased`` when the document's bytes go. A row carries ids
and hashes, never contents, and never a name that holds the forgotten words. The document store declares
``attachment_redacted`` among its receipts. Adapted from Mac's
``test_forget_writes_an_activity_row_for_each_document_it_changes``, plus the rename-only case Windows'
forget-gaps port added, and the case where the row cannot be written.
"""
from __future__ import annotations

import logging
import random
import string

import pytest

from core import chat_attachments as ca
from core.memory.carriers import carries
from tests.test_question_date_time_leg_20261002 import _hash_backend, _profile

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _chat_id(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _attach(chat: str, turn: str, text: str, name: str = "") -> dict:
    doc = ca.stage_document(session_id=chat, data=text.encode("utf-8"), declared_name=name)
    ca.bind_to_turn(session_id=chat, turn_id=turn, attachment_ids=[doc["id"]])
    ca.release_turn(session_id=chat, turn_id=turn, outcomes=None)
    return doc


@pytest.fixture
def rows(monkeypatch):
    import core.runtime_task_events as events

    seen: list[dict] = []
    monkeypatch.setattr(
        events, "emit_runtime_event",
        lambda ctx, *, event_type, message, details=None: seen.append(
            {"type": event_type, "message": message, "session": ctx.get("session_id"), **(details or {})}),
    )
    return seen


def test_forget_writes_an_activity_row_for_each_document_it_changes(tmp_path, rows):
    rng = random.Random(511)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    first, second = _word(rng, 6).upper(), _word(rng, 6).upper()
    redacted = _attach(chat, "turn-1", f"Line one.\nThe shed code is {value}.\nLine three.\n")
    erased = _attach(chat, "turn-2", f"The lamp phrase is {first}\n{second} and that is all.\n")

    assert ca.forget_in_documents(chat, value) == 1
    assert len(rows) == 1 and rows[0]["type"] == "attachment_redacted", rows
    assert rows[0]["attachment_id"] == redacted["id"] and rows[0]["session"] == chat
    assert rows[0]["sha256_before"] == redacted["sha256"] != rows[0]["sha256_after"]
    assert rows[0]["sha256_after"] == ca._read_manifest(redacted["id"])["sha256"]
    assert value not in repr(rows)

    # A value split across lines survives a line rewrite: the bytes are erased instead.
    assert ca.forget_in_documents(chat, f"{first} {second}") == 1
    assert rows[-1]["type"] == "attachment_erased" and rows[-1]["attachment_id"] == erased["id"], rows
    assert rows[-1]["sha256_before"] == erased["sha256"] and rows[-1]["sha256_after"] == ""
    assert first not in repr(rows) and second not in repr(rows)
    assert {row["type"] for row in rows} <= set(ca.BLACKBOX_DOCUMENT_STORE_CAPABILITY["receipts"])


def test_a_rename_only_forget_writes_a_row_that_names_the_new_name(tmp_path, rows):
    rng = random.Random(512)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    doc = _attach(chat, "turn-1", "Nothing secret in here.\n", name=f"key-{value}.txt")

    assert ca.forget_in_documents(chat, value) == 1
    renamed = ca._read_manifest(doc["id"])["name"]
    assert not carries(renamed, value)
    assert len(rows) == 1 and rows[0]["type"] == "attachment_redacted" and rows[0]["attachment_id"] == doc["id"], rows
    assert renamed in rows[0]["message"]
    # The bytes did not change: only the name did.
    assert rows[0]["sha256_before"] == rows[0]["sha256_after"] == doc["sha256"]
    assert value not in repr(rows)


def test_the_document_store_declares_the_redacted_receipt():
    assert "attachment_redacted" in ca.BLACKBOX_DOCUMENT_STORE_CAPABILITY["receipts"]


def test_a_row_that_cannot_be_written_leaves_the_forget_standing(tmp_path, monkeypatch, caplog):
    import core.runtime_task_events as events

    rng = random.Random(513)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    doc = _attach(chat, "turn-1", f"Line one.\nThe shed code is {value}.\nLine three.\n")
    calls: list[str] = []

    def refuse(ctx, *, event_type, message, details=None):
        calls.append(event_type)
        raise OSError(f"the activity store is gone ({message})")

    monkeypatch.setattr(events, "emit_runtime_event", refuse)
    with caplog.at_level(logging.WARNING):
        assert ca.forget_in_documents(chat, value) == 1
    assert calls == ["attachment_redacted"]
    assert value not in (ca.stage_dir() / f"{doc['id']}.bin").read_text(encoding="utf-8")
    warned = [record.getMessage() for record in caplog.records if "Activity row" in record.getMessage()]
    assert warned and doc["id"] in warned[0] and "OSError" in warned[0], warned
    assert not any(value in record.getMessage() for record in caplog.records)
