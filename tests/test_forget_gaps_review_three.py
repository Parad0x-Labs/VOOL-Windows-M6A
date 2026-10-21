"""Review 3 of the forget-gaps branch (REVIEW-3.md), pinned failing-first.

W1  the withdrawn-subject match (stemmed, fuzzy) renamed the user's own file names in storage on any
    later forget: ``documentation.txt`` after "Forget the document", and a ``plan.txt`` uploaded
    after "Forget the plan". It belongs to names a forget wrote; a user's own name is hidden from
    the model by the serve screen only, as before this branch.
T1  the verifier's own check of a name against EARLIER forgotten words was not pinned directly.
Nit when no neutral name clears, the forget reports incomplete before it writes anything.
"""
from __future__ import annotations

import random

import pytest

from core import chat_attachments as ca
from core.memory.carriers import carries
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend


def _canonical(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _value(rng: random.Random) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5)) + f"-{rng.randint(1000, 9999)}"


def _attach(chat: str, turn: str, text: str, name: str) -> dict:
    doc = ca.stage_document(session_id=chat, data=text.encode("utf-8"), declared_name=name)
    ca.bind_to_turn(session_id=chat, turn_id=turn, attachment_ids=[doc["id"]])
    ca.release_turn(session_id=chat, turn_id=turn, outcomes=None)
    return doc


@pytest.fixture
def receipts(monkeypatch):
    import core.persistent_memory as pm

    seen: list[dict] = []
    original = pm._forget_outcomes.record

    def record(response, **kwargs):
        seen.append({"response": str(response), "stores": dict(kwargs.get("stores") or {})})
        return original(response, **kwargs)

    monkeypatch.setattr(pm._forget_outcomes, "record", record)
    return seen


# --- W1 ----------------------------------------------------------------------------------------------

def test_a_users_own_name_naming_a_withdrawn_subject_keeps_its_name_in_storage(tmp_path, receipts):
    rng = random.Random(5501)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "The plan is to leave early.")])
    before = _attach(chat, "turn-1", "Chapter one.\n", "documentation.txt")
    handled, reply = _forget_command(profile, chat, "Forget the plan")
    assert reply.startswith("Forget applied"), reply
    after = _attach(chat, "turn-2", "Q3 roadmap.\n", "plan.txt")
    assert ca._read_manifest(after["id"])["name"] == "plan.txt"

    _ingest(profile, chat, [(_ts("2026-03-06T09:00:00"), "I left the document on the kitchen table.")])
    handled, reply = _forget_command(profile, chat, "Forget the document")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "verified"
    assert ca._read_manifest(before["id"])["name"] == "documentation.txt"
    assert ca._read_manifest(after["id"])["name"] == "plan.txt"


def test_the_serve_screen_still_hides_a_users_name_naming_a_withdrawn_subject(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(5503)
    _profile(tmp_path)
    chat = _canonical(rng)
    doc = _attach(chat, "turn-1", "Q3 roadmap.\n", "plan.txt")
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *_a, **_k: ("the plan",))
    names = [e.get("name") for e in ca.carried_documents_for_turn(session_id=chat, exclude_turn_id="turn-2")]
    assert names and "plan.txt" not in names, names
    assert ca._read_manifest(doc["id"])["name"] == "plan.txt"


def test_a_forget_written_name_naming_a_withdrawn_subject_is_still_renamed(tmp_path, receipts):
    """N1 stays fixed: the withdrawn-subject clause still applies to names a forget wrote."""
    rng = random.Random(5504)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    doc = _attach(chat, "turn-1", "Orchard notes.\n", f"code-{value}.txt")
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert reply.startswith("Forget applied"), reply
    assert ca._read_manifest(doc["id"])["name"].startswith("document-")
    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), "I left the document on the kitchen table.")])
    handled, reply = _forget_command(profile, chat, "Forget the document")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))
    assert not ca._read_manifest(doc["id"])["name"].startswith("document-")


# --- T1 ----------------------------------------------------------------------------------------------

def test_the_verifier_flags_a_name_carrying_an_earlier_forgotten_word(tmp_path, receipts):
    """Direct: no forget runs after the bad name is written, so no rename can repair it first."""
    rng = random.Random(5505)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "My notes are in a design document.")])
    doc = _attach(chat, "turn-1", "Garden notes.\n", "garden.txt")
    other = _attach(chat, "turn-2", "Plum notes.\n", "plums.txt")
    handled, reply = _forget_command(profile, chat, "Forget document")
    assert reply.startswith("Forget applied"), reply
    manifest = ca._read_manifest(doc["id"])
    manifest["name"] = f"design-document-{doc['id'][4:12]}.txt"  # a user's own name: no forget wrote it
    ca._write_manifest(manifest)
    assert ca.documents_carrying(chat, "unrelatedword") == [doc["id"]]
    assert other["id"] not in ca.documents_carrying(chat, "unrelatedword")


# --- Nit ---------------------------------------------------------------------------------------------

def test_a_forget_with_no_clean_name_reports_incomplete_before_writing(tmp_path, receipts, monkeypatch):
    rng = random.Random(5506)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    docs = [_attach(chat, f"turn-{i}", f"Notes {i}.\n", f"note{i}-{value}.txt") for i in range(2)]
    real, calls = ca._neutral_name, []

    def second_has_no_clean_name(record, token="", *, clear=None):
        calls.append(record["id"])
        return ca._WORDLESS_NAME if len(calls) == 2 else real(record, token, clear=clear)

    monkeypatch.setattr(ca, "_neutral_name", second_has_no_clean_name)
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and not reply.startswith("Forget applied"), reply
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "incomplete"
    names = [ca._read_manifest(doc["id"])["name"] for doc in docs]
    assert sorted(names) == sorted(
        [f"note0-{value}.txt", f"note1-{value}.txt"]), f"a record was renamed before the forget gave up: {names}"
    assert all(carries(name, value) for name in names)
