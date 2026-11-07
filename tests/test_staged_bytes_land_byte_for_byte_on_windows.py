"""Staged attachment and deliverable bytes land on disk, and come back, byte for byte.

On Windows os.open without O_BINARY opens the descriptor in text mode, and os.fdopen(fd, "wb")
keeps that mode: a write turns every 0x0A into 0x0D 0x0A, and a read turns 0x0D 0x0A back into
0x0A and stops at the first 0x1A. An attachment or a deliverable is arbitrary bytes (images,
PDFs, archives), so either direction silently corrupts it. POSIX has no text mode; there these
tests pin the behaviour that already holds.
"""
from __future__ import annotations

import pytest

from core import chat_attachments, deliverables

_AWKWARD = b"PNG\r\n\x1a\nline\nCRLF\r\nlone CR\r end\x1a\x00\xff"


def test_a_staged_attachment_is_written_byte_for_byte(tmp_path) -> None:
    target = tmp_path / "a.bin"
    chat_attachments._write_atomic(target, _AWKWARD)
    assert target.read_bytes() == _AWKWARD


def test_a_staged_attachment_reads_back_byte_for_byte(tmp_path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(_AWKWARD)
    assert chat_attachments._open_regular(target) == _AWKWARD


def test_a_deliverable_is_written_byte_for_byte(tmp_path) -> None:
    target = tmp_path / "d.bin"
    deliverables._write_private(target, _AWKWARD)
    assert target.read_bytes() == _AWKWARD


def test_a_deliverable_reads_back_byte_for_byte(tmp_path) -> None:
    target = tmp_path / "d.bin"
    target.write_bytes(_AWKWARD)
    assert deliverables._read_regular(target) == _AWKWARD


@pytest.mark.parametrize("module", [chat_attachments, deliverables], ids=["attachment", "deliverable"])
def test_a_round_trip_through_the_store_keeps_every_byte(tmp_path, module) -> None:
    target = tmp_path / "r.bin"
    writer = module._write_atomic if module is chat_attachments else module._write_private
    reader = module._open_regular if module is chat_attachments else module._read_regular
    writer(target, _AWKWARD)
    assert reader(target) == _AWKWARD
