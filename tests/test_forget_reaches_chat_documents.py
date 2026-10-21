"""A "forget" reaches the chat's documents: what the user forgot is never carried back in from one.

Before this law (FIX-PLAN 1.1, "Forget vs documents"), the forget executor never touched
``core.chat_attachments``: a fact stated in a chat AND present in a document attached to that chat
answered "Forget applied", yet the document's exact text, carried into every later turn by
``carried_documents_for_turn``, still held it. NO RECORD = REFUSE cannot hold while a carrier keeps
the words. Pinned here: the forget removes the words from the document's bytes, the receipt names
the ``chat_documents`` store, a later turn's carried text never holds them, the rest of the
document still arrives, and an unreadable ledger withholds the document instead of serving it.
"""
from __future__ import annotations

import random
import string

import pytest

from core import chat_attachments as ca
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _document_lines(rng: random.Random, secret_line: str, keep_line: str, count: int = 60) -> str:
    lines = [f"{_word(rng).capitalize()} {_word(rng)} {_word(rng)} item {i}." for i in range(count)]
    lines[count // 3] = secret_line
    lines[(2 * count) // 3] = keep_line
    return "\n".join(lines) + "\n"


@pytest.fixture
def receipts(monkeypatch):
    """Every forget outcome's store receipt, as the executor recorded it."""
    import core.persistent_memory as pm

    seen: list[dict] = []
    original = pm._forget_outcomes.record

    def record(response, **kwargs):
        seen.append({"response": str(response), "stores": dict(kwargs.get("stores") or {})})
        return original(response, **kwargs)

    monkeypatch.setattr(pm._forget_outcomes, "record", record)
    return seen


def _attach(chat: str, turn: str, text: str) -> dict:
    doc = ca.stage_document(session_id=chat, data=text.encode("utf-8"))
    ca.bind_to_turn(session_id=chat, turn_id=turn, attachment_ids=[doc["id"]])
    ca.release_turn(session_id=chat, turn_id=turn, outcomes=None)
    return doc


def _carried_text(chat: str, turn: str, question: str) -> str:
    entries = ca.model_attachments_from_source_context(
        {"runtime_session_id": chat, "attachment_turn_id": turn}, question=question,
    )
    return "\n".join(str(entry.get("text") or "") for entry in entries)


def _stage_bytes_all() -> bytes:
    root = ca.stage_dir()
    return b"".join(path.read_bytes() for path in sorted(root.glob("att_*")) if path.is_file())


def test_forget_removes_a_chat_fact_from_the_chats_carried_document(tmp_path, receipts):
    rng = random.Random(8812)
    profile = _profile(tmp_path)
    chat = "forget-docs-" + _word(rng, 10)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    keep = f"{_word(rng, 6)}-{_word(rng, 6)}"
    secret_line = f"The shed padlock code is {value}."
    keep_line = f"The delivery bay is labelled {keep}."
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), secret_line)])
    doc = _attach(chat, "turn-1", _document_lines(rng, secret_line, keep_line))
    assert value in _carried_text(chat, "turn-2", "What is the shed padlock code?")

    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled, reply
    assert reply.startswith("Forget applied"), reply

    stores = receipts[-1]["stores"]
    assert stores.get("chat_documents", {}).get("status") in {"verified", "erased"}, stores

    later = _carried_text(chat, "turn-4", "What is the shed padlock code?")
    assert value.lower() not in later.lower(), later
    assert keep in _carried_text(chat, "turn-4", "Which label is on the delivery bay?")
    assert value.encode() not in _stage_bytes_all()
    record = ca._read_manifest(doc["id"])
    assert record and record.get("original_sha256") == doc["sha256"] and record.get("sha256") != doc["sha256"]


def test_another_chats_document_keeps_its_own_words(tmp_path, receipts):
    rng = random.Random(4401)
    profile = _profile(tmp_path)
    chat_a = "forget-docs-a-" + _word(rng, 8)
    chat_b = "forget-docs-b-" + _word(rng, 8)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    line = f"The archive drawer code is {value}."
    _ingest(profile, chat_a, [(_ts("2026-03-04T09:00:00"), line)])
    _attach(chat_a, "turn-1", _document_lines(rng, line, "Nothing else."))
    _attach(chat_b, "turn-1", _document_lines(rng, line, "Nothing else."))

    handled, reply = _forget_command(profile, chat_a, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    assert value not in _carried_text(chat_a, "turn-3", "archive drawer code")
    assert value in _carried_text(chat_b, "turn-3", "archive drawer code")


def test_a_document_only_value_is_forgotten_and_reported(tmp_path, receipts):
    """The value sits only in the document (never stated as a message): forget still removes it and counts it."""
    rng = random.Random(9902)
    profile = _profile(tmp_path)
    chat = "forget-docs-only-" + _word(rng, 8)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "I attached the yard notes.")])
    _attach(chat, "turn-1", _document_lines(rng, f"The yard gate code is {value}.", "Gate opens at seven."))

    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled, reply
    assert reply.startswith("Forget applied"), reply
    assert value not in _carried_text(chat, "turn-3", "yard gate code")
    assert "Gate opens at seven." in _carried_text(chat, "turn-3", "when does the gate open")


def test_an_unreadable_ledger_withholds_carried_documents(tmp_path, monkeypatch):
    rng = random.Random(3307)
    _profile(tmp_path)
    chat = "forget-docs-ledger-" + _word(rng, 8)
    _attach(chat, "turn-1", _document_lines(rng, "Plain line.", "Another plain line."))
    assert "Plain line." in _carried_text(chat, "turn-2", "plain")

    import core.memory.revocation as revocation

    def unavailable(*args, **kwargs):
        raise RuntimeError("independent forget authority is unavailable")

    monkeypatch.setattr(revocation, "revoked_tokens", unavailable)
    assert "Plain line." not in _carried_text(chat, "turn-2", "plain")


def test_verify_fails_closed_when_a_document_cannot_be_read(tmp_path, monkeypatch):
    from core.memory.carriers import verify_forget_stores

    rng = random.Random(5150)
    _profile(tmp_path)
    chat = "forget-docs-verify-" + _word(rng, 8)
    _attach(chat, "turn-1", _document_lines(rng, "The pier code is PIER-2201.", "Other."))

    erasures: dict = {}
    verify_forget_stores(chat, "PIER-2201", owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "incomplete"

    def broken(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(ca, "documents_carrying", broken)
    erasures = {}
    verify_forget_stores(chat, "Other-unrelated-9", owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "incomplete"


def test_re_pasting_forgotten_words_is_refused_in_that_chat_only(tmp_path, receipts):
    rng = random.Random(6120)
    profile = _profile(tmp_path)
    chat = "forget-docs-repaste-" + _word(rng, 8)
    other = "forget-docs-other-" + _word(rng, 8)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    line = f"The boathouse code is {value}."
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), line)])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply

    with pytest.raises(ca.AttachmentRefused) as refused:
        ca.stage_document(session_id=chat, data=_document_lines(rng, line, "Unrelated.").encode())
    assert refused.value.code == "forgotten_content"
    assert value.encode() not in _stage_bytes_all()
    ca.stage_document(session_id=other, data=_document_lines(rng, line, "Unrelated.").encode())


def test_a_subject_forget_withholds_the_documents_window(tmp_path, receipts):
    """"Forget my locker code" names a subject, not the value: the window stating it is not carried."""
    rng = random.Random(7781)
    profile = _profile(tmp_path)
    chat = "forget-docs-subject-" + _word(rng, 8)
    code = str(rng.randint(1000, 9999))
    line = f"My locker code is {code}."
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), line)])
    _attach(chat, "turn-1", _document_lines(rng, line, "The canteen opens at noon.", count=200))

    handled, reply = _forget_command(profile, chat, "Forget my locker code")
    assert handled, reply
    later = _carried_text(chat, "turn-4", "What is my locker code?")
    assert code not in later, later


def test_a_file_with_no_readable_owner_fails_verification_only_when_it_carries_the_words(tmp_path):
    from core.memory.carriers import verify_forget_stores

    rng = random.Random(2290)
    _profile(tmp_path)
    chat = "forget-docs-orphan-" + _word(rng, 8)
    doc = _attach(chat, "turn-1", _document_lines(rng, "The quay code is QUAY-7310.", "Other."))
    ca._manifest_path(doc["id"]).write_text("{not json", encoding="utf-8")

    erasures: dict = {}
    verify_forget_stores(chat, "QUAY-7310", owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "incomplete"
    erasures = {}
    verify_forget_stores(chat, "UNRELATED-1188", owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "verified"
