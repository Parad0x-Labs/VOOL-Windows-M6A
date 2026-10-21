"""Three forget gaps left by the names commit (2a1f0e8), found by the Windows port's review.

1. A refused upload's Activity row repeated the file's name, and a name can be what was forgotten.
   The run-history writer blanks the row for the chat's own id, but not under another handle of it.
2. The neutral name ``document-<id>`` carries "document" (and "doc", and a piece of the id), so
   "Forget document" in a chat whose renamed record kept that name never verified.
3. At serve time only a document's name was screened: an image's or an artifact's went as stored,
   with a revoked word in it or with the ledger unreadable.
"""
from __future__ import annotations

import json
import random
import string
import struct
import zlib
from urllib.parse import quote

import pytest

from core import chat_attachments as ca
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend


def _png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 5)) + chunk(b"IEND", b"")


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _chat_id(rng: random.Random) -> str:
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


# --- 1. Activity -------------------------------------------------------------------------------------

@pytest.mark.parametrize("handle_kind", ["canonical", "other_handle"])
def test_a_refused_upload_never_writes_the_forgotten_name_to_activity(tmp_path, handle_kind):
    from core.chat_session_identity import canonical_chat_session_id
    from core.runtime_continuity import list_recent_runtime_session_events
    from core.web.api.service import RAW_UPLOAD_PATH, dispatch_upload

    rng = random.Random(9101)
    profile = _profile(tmp_path)
    handle = _chat_id(rng) if handle_kind == "canonical" else "legacy-chat-" + _word(rng, 10)
    chat = canonical_chat_session_id(handle)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply

    headers = {"x-vool-session-id": handle, "x-vool-attachment-name": quote(f"code-{value}.txt"),
               "x-vool-attachment-type": "text/plain"}
    response = dispatch_upload(path=RAW_UPLOAD_PATH, raw_body=b"Clean text.\n", headers=headers, runtime=None,
                               client_host="127.0.0.1")
    assert b"forgotten_content" in response.body and value.encode() not in response.body
    rows = [*list_recent_runtime_session_events(handle, limit=200), *list_recent_runtime_session_events(chat, limit=200)]
    refused = [row for row in rows if row.get("event_type") == "attachment_refused"]
    assert refused, "the refusal left no Activity row"
    assert value.lower() not in json.dumps(rows, ensure_ascii=False).lower()


# --- 2. A neutral name never carries the word it replaced ---------------------------------------------

def test_forget_document_verifies_when_a_name_holds_the_word(tmp_path, receipts):
    rng = random.Random(9102)
    profile = _profile(tmp_path)
    chat = _chat_id(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), "I keep my notes in a design document.")])
    doc = _attach(chat, "turn-1", "Garden notes.\nWater on Mondays.\n", "design-document.txt")

    handled, reply = _forget_command(profile, chat, "Forget document")
    assert handled and reply.startswith("Forget applied"), reply
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "verified", receipts[-1]["stores"]["chat_documents"]
    name = ca._read_manifest(doc["id"])["name"]
    assert "document" not in name.lower() and name.endswith(".txt"), name


@pytest.mark.parametrize("second", ["document", "doc", "id_piece"])
def test_a_later_forget_of_the_neutral_names_words_verifies(tmp_path, receipts, second):
    """The realistic case: an earlier forget already renamed a record of the chat."""
    rng = random.Random(9105)
    profile = _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    doc = _attach(chat, "turn-1", "Garden notes.\nWater on Mondays.\n", f"code-{value}.txt")
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    first = ca._read_manifest(doc["id"])["name"]
    assert first == f"document-{doc['id'][4:12]}.txt", first  # the usual neutral name is unchanged

    token = doc["id"][4:12] if second == "id_piece" else second
    _ingest(profile, chat, [(_ts("2026-03-05T09:00:00"), f"Note the word {token} here.")])
    handled, reply = _forget_command(profile, chat, f"Forget {token}")
    assert handled and reply.startswith("Forget applied"), reply
    assert receipts[-1]["stores"]["chat_documents"]["status"] == "verified", receipts[-1]["stores"]["chat_documents"]
    assert not ca.documents_carrying(chat, token)
    assert token.lower() not in ca._read_manifest(doc["id"])["name"].lower()


def test_the_neutral_name_skips_any_candidate_carrying_the_token():
    record = {"id": "att_0123456789abcdef0123456789abcdef", "name": "x.txt"}
    assert ca._neutral_name(record, "secret") == "document-01234567.txt"
    for token in ("document", "doc", "01234567"):
        name = ca._neutral_name(record, token)
        assert not ca._carries(name, token), (token, name)
    assert ca._neutral_name(record) == "document-01234567"


# --- 3. Every kind's name is screened at serve time ---------------------------------------------------

def _bound_kinds(chat: str, value: str) -> dict[str, dict]:
    image = ca.stage_attachment(session_id=chat, declared_name=f"scan-{value}.png", declared_type="image/png", data=_png())
    picked = ca.stage_attachment(session_id=chat, declared_name=f"list-{value}.txt", declared_type="text/plain",
                                 data=b"Plain picked text.\n")
    artifact = ca.stage_attachment(session_id=chat, declared_name=f"deck-{value}.png", declared_type="image/png", data=_png())
    manifest = ca._read_manifest(artifact["id"])
    manifest["kind"] = ca.KIND_ARTIFACT  # an extracted artifact; its reader is not under test here
    ca._write_manifest(manifest)
    doc = ca.stage_document(session_id=chat, data=b"Harmless line.\n", declared_name=f"note-{value}.txt")
    records = {"image": image, "picked": picked, "artifact": artifact, "document": doc}
    ca.bind_to_turn(session_id=chat, turn_id="turn-1", attachment_ids=[r["id"] for r in records.values()])
    return records


@pytest.mark.parametrize("ledger", ["unreadable", "revoked"])
def test_every_kind_of_name_is_screened_at_serve_time(tmp_path, monkeypatch, ledger):
    import core.memory.revocation as revocation

    rng = random.Random(9103)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    records = _bound_kinds(chat, value)
    if ledger == "unreadable":
        def unreadable(*_a, **_k):
            raise RuntimeError("the ledger cannot be read")

        monkeypatch.setattr(revocation, "revoked_tokens", unreadable)
    else:
        monkeypatch.setattr(revocation, "revoked_tokens", lambda *_a, **_k: (value,))
    served = [
        *ca.model_attachments_for_turn(session_id=chat, turn_id="turn-1"),
        *ca.evidence_items_for_turn(session_id=chat, turn_id="turn-1"),
    ]
    names = [str(entry.get("name") or "") for entry in served]
    # Windows withholds every text record while the ledger cannot be read, the picked file included
    # (Mac serves its bytes unscreened under a neutral name, from 0fec732, which is not ported).
    kinds = ("image", "picked", "artifact") if ledger == "revoked" else ("image", "artifact")
    assert len(names) >= 2 * len(kinds), names  # each served kind from both lanes at least
    assert not any(value.lower() in name.lower() for name in names), names
    for kind in kinds:
        assert ca._neutral_name(records[kind]) in names, (kind, names)
    if ledger == "unreadable":
        assert ca._neutral_name(records["picked"]) not in names, names


def test_an_unrevoked_name_is_served_as_it_is(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(9106)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    records = _bound_kinds(chat, value)
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *_a, **_k: ("unrelated",))
    names = {str(entry.get("name") or "") for entry in ca.model_attachments_for_turn(session_id=chat, turn_id="turn-1")}
    assert {r["name"] for r in records.values()} <= names, names


def test_forget_renames_a_staged_image(tmp_path, receipts):
    rng = random.Random(9104)
    profile = _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    image = ca.stage_attachment(session_id=chat, declared_name=f"scan-{value}.png", declared_type="image/png", data=_png())
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    assert value.lower() not in ca._read_manifest(image["id"])["name"].lower()
