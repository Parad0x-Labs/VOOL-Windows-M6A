"""Windows read fidelity for staged chat documents: the bytes that went in are the bytes
that come back -- through the store's lowest-level reader, the preview door and the model
payload reader.

Measured 2026-10-29 (glm-diag-crlf-fidelity): ``os.open`` without ``O_BINARY`` hands back
a text-mode descriptor on Windows and ``os.read`` folds every CR-LF into LF, so pasted
documents reached the model with their line endings rewritten. The store's WRITE side is
binary (``os.fdopen(fd, "wb")``); these tests pin the READ side to the same standard. On
POSIX ``O_BINARY`` does not exist and every assertion holds either way.
"""

from __future__ import annotations

import pytest

from core import runtime_paths
from core.chat_attachments import (
    _open_regular,
    bind_to_turn,
    model_attachments_for_turn,
    read_staged_bytes,
    stage_document,
)

SESSION = "openclaw:d0c0d0c0d0c0d0c0f1de"
TEXT = "start line\r\n\r\n\tindented with a tab\r\n    four spaces\r\nlast line\r\n"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def test_the_store_reader_returns_exact_crlf_bytes(tmp_path):
    planted = tmp_path / "planted.bin"
    exact = b"one\r\ntwo\r\n\r\nthree\r\n"
    planted.write_bytes(exact)
    assert _open_regular(planted) == exact


def test_read_staged_bytes_serves_the_exact_document_bytes():
    record = stage_document(session_id=SESSION, data=TEXT.encode("utf-8"), declared_name="notes.txt")
    served = read_staged_bytes(session_id=SESSION, attachment_id=record["id"])
    assert served is not None
    assert served[1] == TEXT.encode("utf-8")


def test_the_model_payload_keeps_crlf_and_blank_lines():
    record = stage_document(session_id=SESSION, data=TEXT.encode("utf-8"), declared_name="notes.txt")
    bind_to_turn(session_id=SESSION, turn_id="turn-crlf", attachment_ids=[record["id"]])
    entries = model_attachments_for_turn(session_id=SESSION, turn_id="turn-crlf")
    delivered = "\n".join(str(entry.get("text") or "") for entry in entries)
    assert "start line\r\n\r\n\tindented with a tab" in delivered
