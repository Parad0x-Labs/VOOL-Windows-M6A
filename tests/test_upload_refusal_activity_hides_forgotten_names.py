"""Every upload refusal keeps a forgotten word out of its Activity row (fix 4 of the forget gaps).

The forgotten_content refusal already uses a neutral label. The other refusals (empty, too large,
credential-shaped, an unreadable image, binary behind a text extension, a dropped document of an
unsupported type) are raised before the forget fence runs, so they name the file in their message.
When the name carries a word this chat asked to forget, or the forget ledger cannot be read, the row
must say "an attachment" / "pasted text" instead -- in the attachment-event path itself, not by
relying on a later writer to blank the row (a port without that writer must stay covered).
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


def _bad_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + b"IHDR" + header + b"\x00\x00\x00\x00"


def _word(rng: random.Random, n: int = 7) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


#: (case, file name pattern, mime type, body, source header, expected refusal code)
CASES = [
    ("empty", "code-{v}.txt", "text/plain", b"", "", "empty_file"),
    ("too_large", "code-{v}.txt", "text/plain", b"x" * 64, "", "too_large"),
    ("credential", "code-{v}.txt", "text/plain", b"aws_access_key_id = AKIA4T7NCXKDTL5TQX3P\n", "", None),
    ("unreadable_image", "scan-{v}.png", "image/png", _bad_png(), "", "image_unparseable"),
    ("binary_as_text", "code-{v}.txt", "text/plain", b"\x00\x01\x02\xff\xfe" * 20, "", "not_text"),
    ("dropped_unsupported", "code-{v}.exe", "application/octet-stream", b"plain words\n", "drop", None),
]


def _upload(handle: str, name: str, mime: str, body: bytes, source: str):
    from core.web.api.service import RAW_UPLOAD_PATH, dispatch_upload

    headers = {"x-vool-session-id": handle, "x-vool-attachment-name": quote(name), "x-vool-attachment-type": mime}
    if source:
        headers["x-vool-attachment-source"] = source
    return dispatch_upload(path=RAW_UPLOAD_PATH, raw_body=body, headers=headers, runtime=None, client_host="127.0.0.1")


def _refused_rows(*handles: str) -> list[dict]:
    from core.runtime_continuity import list_recent_runtime_session_events

    rows = [row for handle in handles for row in list_recent_runtime_session_events(handle, limit=200)]
    return [row for row in rows if row.get("event_type") == "attachment_refused"]


def _forgotten_chat(tmp_path, rng: random.Random, value: str, handle_kind: str) -> tuple[str, str]:
    from core.chat_session_identity import canonical_chat_session_id

    profile = _profile(tmp_path)
    handle = ("openclaw:" + "".join(rng.choice("0123456789abcdef") for _ in range(20))
              if handle_kind == "canonical" else "legacy-chat-" + _word(rng, 10))
    chat = canonical_chat_session_id(handle)
    _ingest(profile, chat, [(_ts("2026-03-04T09:00:00"), f"The safe code is {value}.")])
    handled, reply = _forget_command(profile, chat, f"Forget {value}")
    assert handled and reply.startswith("Forget applied"), reply
    return handle, chat


@pytest.mark.parametrize("handle_kind", ["canonical", "other_handle"])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_a_refusal_never_names_a_file_whose_name_was_forgotten(tmp_path, monkeypatch, case, handle_kind):
    label, pattern, mime, body, source, code = case
    if label == "too_large":
        monkeypatch.setattr(ca, "MAX_BYTES_PER_FILE", 16)
    rng = random.Random(9201)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    handle, chat = _forgotten_chat(tmp_path, rng, value, handle_kind)

    response = _upload(handle, pattern.format(v=value), mime, body, source)
    assert response.status >= 400, response
    if code:
        assert code.encode() in response.body, response.body[:300]
    rows = _refused_rows(handle, chat)
    assert rows, "the refusal left no Activity row"
    assert value.lower() not in json.dumps(rows, ensure_ascii=False).lower(), rows


def test_a_refusal_names_the_file_when_its_name_carries_nothing_forgotten(tmp_path):
    rng = random.Random(9202)
    value = f"{_word(rng, 5).upper()}-{rng.randint(1000, 9999)}"
    handle, chat = _forgotten_chat(tmp_path, rng, value, "other_handle")
    response = _upload(handle, "garden-notes.txt", "text/plain", b"", "")
    assert b"empty_file" in response.body
    assert "garden-notes.txt" in json.dumps(_refused_rows(handle, chat), ensure_ascii=False)


def test_an_unreadable_ledger_hides_the_name(tmp_path, monkeypatch):
    import core.memory.revocation as revocation

    rng = random.Random(9203)
    _profile(tmp_path)
    handle = "legacy-chat-" + _word(rng, 10)

    def unreadable(*_a, **_k):
        raise RuntimeError("the ledger cannot be read")

    monkeypatch.setattr(revocation, "revoked_tokens", unreadable)
    response = _upload(handle, "private-plans.txt", "text/plain", b"", "")
    assert b"empty_file" in response.body
    rows = _refused_rows(handle)
    assert rows and "private-plans" not in json.dumps(rows, ensure_ascii=False), rows
