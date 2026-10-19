"""Framing and failure types for the Windows broker control protocol."""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_LENGTH = struct.Struct("<I")


class ProtocolError(ValueError):
    """The peer sent an invalid or incomplete protocol message."""


class BrokerRefusal(RuntimeError):
    """The broker refused a request without applying its effect."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


class EffectOutcomeUnknown(RuntimeError):
    """A request was written but its effect could not be determined."""


def encode_frame(message: dict[str, Any]) -> bytes:
    """Encode one UTF-8 JSON object with a four-byte little-endian length."""
    try:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError("message is not valid UTF-8 JSON") from exc
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds 64 MiB")
    return _LENGTH.pack(len(payload)) + payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            raise EOFError("broker channel closed during a message")
        chunks.extend(part)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> dict[str, Any]:
    """Read and validate one framed JSON object from a binary stream."""
    size = _LENGTH.unpack(_read_exact(stream, _LENGTH.size))[0]
    if size > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds 64 MiB")
    try:
        value = json.loads(_read_exact(stream, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return value

