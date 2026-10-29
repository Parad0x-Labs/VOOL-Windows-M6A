"""Review 2 of the forget-gaps branch (REVIEW-2.md, N1-N5), pinned failing-first.

N1  "Forget the document" withdraws a renamed ``document-<id>.txt`` (the verifier's test) but the
    rename used plain ``carries``: the forget never verified, nor did any later one, and the chat's
    documents were withheld from then on.
N2  a record renamed before this branch into an earlier forgotten word blocked every later forget;
    and ``_forget_activity`` wrote such a name.
N3  every record that fell back got the same name ``…``, without its extension.
N4  with N1 fixed, a refusal's Activity row keeps its useful text once nothing is pending.
N5  the forget ledger is read under the forget authority's lock.
"""
from __future__ import annotations

import contextlib
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


def _events(chat: str) -> list[dict]:
    from core.runtime_continuity import list_recent_runtime_session_events

    return list_recent_runtime_session_events(chat, limit=500)


# --- N1 (and N4) -------------------------------------------------------------------------------------

def test_a_subject_forget_after_a_rename_applies_and_the_chat_keeps_working(tmp_path, receipts):
    from urllib.parse import quote

    from core.web.api.service import RAW_UPLOAD_PATH, dispatch_upload

    rng = random.Random(4401)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value, other = _value(rng), _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    doc = _attach(chat, "turn-1", "Orchard notes.\n", f"code-{value}.txt")
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert reply.startswith("Forget applied"), reply

    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), "I left the document on the kitchen table.")])
    handled, reply = _forget_command(profile, chat, "Forget the document")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "verified"

    _ingest(profile, chat, [(_ts("2026-03-06T09:00:00"), f"My locker is {other}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {other}")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))

    name = ca._read_manifest(doc["id"])["name"]
    assert not carries(name, value) and not carries(name, "document"), name
    later = ca.stage_document(session_id=chat, data=b"Plum notes.\n", declared_name="plums.txt")
    ca.bind_to_turn(session_id=chat, turn_id="turn-9", attachment_ids=[later["id"]])
    assert [e.get("name") for e in ca.model_attachments_for_turn(session_id=chat, turn_id="turn-9")] == ["plums.txt"]

    # N4: nothing is pending any more, so a refusal's row keeps its useful text.
    headers = {"x-vool-session-id": chat, "x-vool-attachment-name": quote("holiday.txt"),
               "x-vool-attachment-type": "text/plain"}
    dispatch_upload(path=RAW_UPLOAD_PATH, raw_body=b"", headers=headers, runtime=None, client_host="127.0.0.1")
    refused = [row for row in _events(chat) if row.get("event_type") == "attachment_refused"]
    assert refused and "holiday.txt" in refused[-1]["message"], refused


# --- N2 ----------------------------------------------------------------------------------------------

def test_a_name_renamed_before_this_branch_is_healed_by_the_next_forget(tmp_path, receipts):
    rng = random.Random(4402)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    v1, v2 = _value(rng), _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "My notes are in a design document.")])
    doc = _attach(chat, "turn-1", f"Garden notes.\nThe gate code is {v1}.\n", "design-document.txt")
    handled, reply = _forget_command(profile, chat, "Forget document")
    assert reply.startswith("Forget applied"), reply
    manifest = ca._read_manifest(doc["id"])
    manifest["name"] = f"document-{doc['id'][4:12]}.txt"  # what the pre-branch rename wrote
    ca._write_manifest(manifest)

    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), f"The code is {v1}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {v1}")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))
    _ingest(profile, chat, [(_ts("2026-03-06T09:00:00"), f"The code is {v2}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {v2}")
    assert reply.startswith("Forget applied"), (reply, receipts[-1]["stores"].get("chat_documents"))
    assert not carries(ca._read_manifest(doc["id"])["name"], "document")

    rows = [row for row in _events(chat) if row.get("event_type") in {"attachment_redacted", "attachment_erased"}]
    assert rows, "the forget left no Activity row for the document"
    for row in rows:
        assert not carries(row["message"].replace("Forget ", ""), "document"), row["message"]


# --- N3 ----------------------------------------------------------------------------------------------

def test_the_fallback_name_is_distinct_per_record_keeps_its_extension_and_is_never_wordless():
    records = [{"id": f"att_{i:032x}", "name": f"notes-{i}.txt"} for i in range(1, 4)]
    blocked = ("document", "file", "attachment", "item")

    def clear(candidate: str) -> bool:
        return not any(carries(candidate, word) for word in blocked)

    names = [ca._neutral_name(record, "notes", clear=clear) for record in records]
    assert len(set(names)) == len(names), names
    for name in names:
        assert name.endswith(".txt") and any(ch.isalnum() for ch in name[:-4]), names
        assert clear(name), name


def test_the_fallback_is_never_the_wordless_name_in_a_forget(tmp_path, receipts, monkeypatch):
    rng = random.Random(4403)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), f"The safe code is {value}.")])
    docs = [_attach(chat, f"turn-{i}", f"Notes {i}.\n", n)
            for i, n in enumerate([f"code-{value}.txt", f"{value} notes.txt", f"list {value}.txt"])]
    real = ca.forgotten_words

    def with_generic_words(session_id, *, settled=True):
        return tuple(sorted({*real(session_id, settled=settled), "document", "file", "attachment", "item"}))

    monkeypatch.setattr(ca, "forgotten_words", with_generic_words)
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    names = [ca._read_manifest(doc["id"])["name"] for doc in docs]
    assert len(set(names)) == 3 and "…" not in names, names
    assert all(name.endswith(".txt") for name in names), names


# --- N5 ----------------------------------------------------------------------------------------------

def test_the_ledger_is_read_under_the_forget_authoritys_lock(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(4405)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert reply.startswith("Forget applied"), reply

    held: list[bool] = []
    original_read, original_serialized = revocation._read, revocation.serialized
    depth = [0]

    @contextlib.contextmanager
    def tracked(*args, **kwargs):
        depth[0] += 1
        try:
            with original_serialized(*args, **kwargs):
                yield
        finally:
            depth[0] -= 1

    def read(directory):
        held.append(depth[0] > 0)
        return original_read(directory)

    monkeypatch.setattr(revocation, "serialized", tracked)
    monkeypatch.setattr(revocation, "_read", read)
    assert ca.forgotten_words(chat, settled=False), "the forget left no ledger row"
    assert held and all(held), held
