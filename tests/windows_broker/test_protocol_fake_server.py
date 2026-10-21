"""Pure-Python protocol checks that also run on non-Windows CI."""

from __future__ import annotations

import io
import struct

import pytest

from core.windows_broker.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    encode_frame,
    read_frame,
)


def test_round_trip_unicode_and_partial_reads() -> None:
    frame = encode_frame({"id": "a", "text": "D’Angelo / 雪"})

    class OneByteReads(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(1 if size > 0 else size)

    assert read_frame(OneByteReads(frame)) == {"id": "a", "text": "D’Angelo / 雪"}


def test_rejects_non_object_and_oversized_frames() -> None:
    with pytest.raises(ProtocolError, match="JSON object"):
        read_frame(io.BytesIO(struct.pack("<I", 2) + b"[]"))
    with pytest.raises(ProtocolError, match="64 MiB"):
        read_frame(io.BytesIO(struct.pack("<I", MAX_MESSAGE_BYTES + 1)))


def test_rejects_invalid_utf8_and_truncated_frame() -> None:
    with pytest.raises(ProtocolError, match="UTF-8"):
        read_frame(io.BytesIO(struct.pack("<I", 1) + b"\xff"))
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(struct.pack("<I", 5) + b"{}"))

