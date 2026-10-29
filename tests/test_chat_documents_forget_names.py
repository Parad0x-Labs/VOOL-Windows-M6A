"""A document's NAME is content to the forget law (third review of the long-docs branch).

The name rides every later turn ("[Document pasted earlier in this chat: code-QZXWV-7731.txt ...]")
and every receipt. Pinned here: forget renames a record whose name carries the value (erased ones
included) and counts it; the verifier reads names; the serve screen hands the model a neutral name
when the name carries a revoked word; staging refuses a name carrying a forgotten value, without
echoing it; and the screen's own "withheld" notice never counts as the user's words.
"""
from __future__ import annotations

import random
import string

import pytest

from core import chat_attachments as ca
from tests.test_forgetting_is_applied_or_never_claimed_20261007 import _forget_command
from tests.test_question_date_time_leg_20261002 import _hash_backend, _ingest, _profile, _ts

pytestmark = pytest.mark.usefixtures("_hash_backend")
assert _hash_backend


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _chat_id(rng: random.Random) -> str:
    return "openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))


def _value(rng: random.Random) -> str:
    return f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"


def _attach(chat: str, turn: str, text: str, name: str = "") -> dict:
    doc = ca.stage_document(session_id=chat, data=text.encode("utf-8"), declared_name=name)
    ca.bind_to_turn(session_id=chat, turn_id=turn, attachment_ids=[doc["id"]])
    ca.release_turn(session_id=chat, turn_id=turn, outcomes=None)
    return doc


def _carried(chat: str, turn: str, question: str = "") -> list[dict]:
    return ca.carried_documents_for_turn(session_id=chat, exclude_turn_id=turn, question=question)


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


def test_forget_renames_a_document_whose_name_holds_the_value(tmp_path, receipts):
    rng = random.Random(701)
    profile = _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    kept = _attach(chat, "turn-1", "Plain notes about the garden.\nWater on Mondays.\n", name=f"code-{value}.txt")
    gone = _attach(chat, "turn-2", "Other notes.\n", name=f"copy-{value}.md")
    assert ca.erase_document(session_id=chat, attachment_id=gone["id"])

    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    store = receipts[-1]["stores"]["chat_documents"]
    assert store["status"] == "verified" and store["removed"] >= 2, store
    for doc in (kept, gone):
        name = ca._read_manifest(doc["id"])["name"]
        assert value.lower() not in name.lower() and name.startswith("document-"), name
    assert ca._read_manifest(kept["id"])["name"].endswith(".txt")
    later = _carried(chat, "turn-3")
    assert [e["name"] for e in later] == [ca._read_manifest(kept["id"])["name"]]
    assert "Water on Mondays." in later[0]["text"]
    receipt = ca.receipt_for_turn(session_id=chat, turn_id="turn-1") + ca.receipt_for_turn(session_id=chat, turn_id="turn-2")
    assert value.lower() not in repr(receipt).lower()


def test_the_verifier_reads_names(tmp_path):
    from core.memory.carriers import verify_forget_stores

    rng = random.Random(702)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    _attach(chat, "turn-1", "Nothing secret in here.\n", name=f"key-{value}.txt")
    erasures: dict = {}
    verify_forget_stores(chat, value, owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "incomplete"
    assert ca.forget_in_documents(chat, value) == 1
    erasures = {}
    verify_forget_stores(chat, value, owner_memories={}, erasures=erasures)
    assert erasures["stores"]["chat_documents"]["status"] == "verified"


def test_the_serve_screen_hides_a_name_carrying_a_revoked_word(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(703)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    doc = _attach(chat, "turn-1", "Harmless line one.\nHarmless line two.\n", name=f"vault-{value}.txt")
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *a, **k: (value,))
    later = _carried(chat, "turn-2")
    assert len(later) == 1 and value.lower() not in later[0]["name"].lower()
    assert later[0]["name"] == ca._neutral_name({"id": doc["id"], "name": doc["name"]})
    assert "Harmless line two." in later[0]["text"]


def test_staging_refuses_a_name_carrying_a_forgotten_value(tmp_path, receipts):
    rng = random.Random(704)
    profile = _profile(tmp_path)
    chat = _chat_id(rng)
    other = _chat_id(rng)
    value = _value(rng)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The boat code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    for stage in (
        lambda: ca.stage_document(session_id=chat, data=b"Clean text.\n", declared_name=f"code-{value}.txt"),
        lambda: ca.stage_attachment(session_id=chat, declared_name=f"code-{value}.txt", declared_type="text/plain", data=b"Clean text.\n"),
    ):
        with pytest.raises(ca.AttachmentRefused) as refused:
            stage()
        assert refused.value.code == "forgotten_content" and value not in refused.value.message
    assert ca.stage_document(session_id=other, data=b"Clean text.\n", declared_name=f"code-{value}.txt")["name"] == f"code-{value}.txt"


def test_the_withheld_notice_is_not_the_users_words(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(705)
    _profile(tmp_path)
    chat = _chat_id(rng)
    value = _value(rng)
    lines = [f"Ordinary line {i} about the orchard." for i in range(200)]
    lines[120] = f"The cellar code is {value}."
    _attach(chat, "turn-1", "\n".join(lines) + "\n")
    monkeypatch.setattr(revocation, "revoked_tokens", lambda *a, **k: (value, "withheld", "asked"))
    later = _carried(chat, "turn-2")
    assert len(later) == 1, "the whole document was withheld because of the screen's own notice"
    assert value not in later[0]["text"] and lines[0] in later[0]["text"] and "withheld" in later[0]["text"]
