"""Review 1 of the forget-gaps branch (REVIEW-1.md, F1-F6), pinned failing-first.

F1  a later forget renamed a record to a name carrying an EARLIER forgotten word, and both forgets
    reported verified; the verifier must catch such a rename too.
F2  a refusal's reason (an archive member's name) carried a forgotten word while the file's name did not.
F3  with every neutral candidate revoked, the serve screen sent an unchecked ``document-<id>``.
F4  a forget recorded under a legacy handle was not seen by an upload under the chat's canonical id.
F5  the Activity check ignored withdrawn subjects, which the serve screen applies.
F6  the extension was not checked together with the candidate, and no clean candidate raised.
"""
from __future__ import annotations

import io
import json
import random
import string
import zipfile
from urllib.parse import quote

import pytest

from core import chat_attachments as ca
from core.memory.carriers import carries
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend

AWS_LINE = b"aws_access_key_id = AKIA4T7NCXKDTL5TQX3P\n"


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _canonical(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _value(rng: random.Random) -> str:
    return f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"


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


def _upload(handle: str, name: str, mime: str, body: bytes):
    from core.web.api.service import RAW_UPLOAD_PATH, dispatch_upload

    headers = {"x-vool-session-id": handle, "x-vool-attachment-name": quote(name), "x-vool-attachment-type": mime}
    return dispatch_upload(path=RAW_UPLOAD_PATH, raw_body=body, headers=headers, runtime=None, client_host="127.0.0.1")


def _refused(*handles: str) -> list[dict]:
    from core.runtime_continuity import list_recent_runtime_session_events

    rows = [row for handle in handles for row in list_recent_runtime_session_events(handle, limit=200)]
    return [row for row in rows if row.get("event_type") == "attachment_refused"]


# --- F1 ----------------------------------------------------------------------------------------------

def test_a_later_rename_never_brings_back_an_earlier_forgotten_word(tmp_path, receipts):
    rng = random.Random(9301)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "My notes are in a design document.")])
    first = _attach(chat, "turn-1", "Garden notes.\n", "design-document.txt")
    handled, reply = _forget_command(profile, chat, "Forget document")
    assert handled and reply.startswith("Forget applied"), reply

    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), f"The safe code is {value}.")])
    second = _attach(chat, "turn-2", "Orchard notes.\n", f"code-{value}.txt")
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    for doc in (first, second):
        name = ca._read_manifest(doc["id"])["name"]
        assert not carries(name, "document") and not carries(name, value), name
    assert not ca.documents_carrying(chat, "document")


def test_the_verifier_catches_a_renamed_name_carrying_an_earlier_forgotten_word(tmp_path, receipts):
    rng = random.Random(9302)
    profile = _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "My notes are in a design document.")])
    doc = _attach(chat, "turn-1", "Garden notes.\n", "design-document.txt")
    handled, reply = _forget_command(profile, chat, "Forget document")
    assert handled and reply.startswith("Forget applied"), reply
    # A rename that put the earlier word back (what the pre-fix rename did on the next forget).
    manifest = ca._read_manifest(doc["id"])
    manifest["name"] = f"document-{doc['id'][4:12]}.txt"
    ca._write_manifest(manifest)

    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "incomplete", receipts[-1]["stores"]["chat_documents"]
    assert not reply.startswith("Forget applied"), reply


def test_with_no_clean_candidate_the_name_holds_no_word():
    record = {"id": "att_0123456789abcdef0123456789abcdef", "name": "x.txt"}
    name = ca._neutral_name(record, "x", clear=lambda _candidate: False)
    assert not any(ch.isalnum() for ch in name), name


# --- F2 ----------------------------------------------------------------------------------------------

def test_a_refusal_reason_carrying_a_forgotten_word_is_not_written(tmp_path):
    rng = random.Random(9303)
    profile = _profile(tmp_path)
    handle = "legacy-chat-" + _word(rng, 10)
    from core.chat_session_identity import canonical_chat_session_id

    chat = canonical_chat_session_id(handle)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"keys-{value}.txt", AWS_LINE)
    response = _upload(handle, "bundle.zip", "application/zip", buffer.getvalue())
    assert response.status >= 400, response.body[:300]
    rows = _refused(handle, chat)
    assert rows, "the refusal left no Activity row"
    text = json.dumps(rows, ensure_ascii=False)
    assert value.lower() not in text.lower(), text
    assert "bundle.zip" in text  # the file's own name carries nothing forgotten and stays


# --- F3 ----------------------------------------------------------------------------------------------

def test_the_serve_screen_never_sends_a_revoked_word_when_every_candidate_is_revoked(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(9304)
    _profile(tmp_path)
    chat = _canonical(rng)
    value = _value(rng)
    doc = ca.stage_document(session_id=chat, data=b"Harmless line.\n", declared_name=f"note-{value}.txt")
    ca.bind_to_turn(session_id=chat, turn_id="turn-1", attachment_ids=[doc["id"]])
    revoked = (value, "document", "file", "attachment", "item")
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *_a, **_k: revoked)
    names = [str(e.get("name") or "") for e in ca.model_attachments_for_turn(session_id=chat, turn_id="turn-1")]
    assert names, "the document was not served"
    assert not any(carries(name, token) for name in names for token in revoked), names


# --- F4 ----------------------------------------------------------------------------------------------

def test_a_forget_under_a_legacy_handle_covers_an_upload_under_the_canonical_id(tmp_path):
    from core.chat_session_identity import canonical_chat_session_id

    rng = random.Random(9305)
    profile = _profile(tmp_path)
    legacy = "legacy-chat-" + _word(rng, 10)
    value = _value(rng)
    _ingest(profile, legacy, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, legacy, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    canonical = canonical_chat_session_id(legacy)
    _upload(canonical, f"code-{value}.txt", "text/plain", b"")
    rows = _refused(canonical, legacy)
    assert rows and value.lower() not in json.dumps(rows, ensure_ascii=False).lower(), rows


# --- F5 ----------------------------------------------------------------------------------------------

def test_a_refused_name_naming_a_withdrawn_subject_is_not_written(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(9306)
    _profile(tmp_path)
    chat = _canonical(rng)
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *_a, **_k: ("the kiln locker code",))
    _upload(chat, "kiln-locker-code.txt", "text/plain", b"")
    rows = _refused(chat)
    assert rows and "kiln" not in json.dumps(rows, ensure_ascii=False).lower(), rows


# --- F6 ----------------------------------------------------------------------------------------------

def test_the_extension_is_checked_with_the_candidate():
    record = {"id": "att_0123456789abcdef0123456789abcdef", "name": "x.txt"}
    for token in ("01234567.txt", "document-01234567.txt"):
        name = ca._neutral_name(record, token)
        assert not carries(name, token), (token, name)
