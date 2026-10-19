"""Synchronous named-pipe client for the Windows broker."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import msvcrt
import os
import secrets
from ctypes import wintypes
from typing import Any

from .protocol import (
    BrokerRefusal,
    EffectOutcomeUnknown,
    ProtocolError,
    encode_frame,
    read_frame,
)


class BrokerClient:
    """A single-request-at-a-time client for an already authenticated pipe."""

    def __init__(self, pipe_name: str, secret: bytes, broker_pid: int) -> None:
        if os.name != "nt":
            raise OSError("the Windows broker is available only on Windows")
        if not pipe_name.startswith(r"\\.\pipe\vool-broker-"):
            raise ValueError("invalid broker pipe name")
        if len(secret) != 32:
            raise ValueError("broker secret must be 256 bits")
        if broker_pid <= 0:
            raise ValueError("invalid broker process id")
        self._pipe_name = pipe_name
        self._secret = secret
        self._broker_pid = broker_pid
        self._stream: Any | None = None

    def connect(self) -> None:
        """Open the broker pipe. The owning process must perform the handshake."""
        if self._stream is not None:
            return
        try:
            # The stream stays open until close() so the caller can make several calls.
            self._stream = open(self._pipe_name, "r+b", buffering=0)  # noqa: SIM115
            self._verify_server_process()
            self._authenticate()
        except OSError as exc:
            self.close()
            raise ConnectionError("broker is unavailable") from exc
        except (EOFError, ProtocolError, ValueError) as exc:
            self.close()
            raise ConnectionError("broker authentication failed") from exc

    def _verify_server_process(self) -> None:
        assert self._stream is not None
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(self._stream.fileno()))
        server_pid = wintypes.ULONG()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_server_pid = kernel32.GetNamedPipeServerProcessId
        get_server_pid.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        get_server_pid.restype = wintypes.BOOL
        if not get_server_pid(handle, ctypes.byref(server_pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        if server_pid.value != self._broker_pid:
            raise OSError("named-pipe server is not the broker process")

    def _authenticate(self) -> None:
        assert self._stream is not None
        challenge = read_frame(self._stream)
        if challenge.get("type") != "challenge":
            raise ProtocolError("missing broker challenge")
        try:
            broker_nonce = bytes.fromhex(challenge["nonce"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("invalid broker challenge") from exc
        if len(broker_nonce) != 32:
            raise ProtocolError("invalid broker challenge")
        client_nonce = secrets.token_bytes(32)
        client_proof = hmac.new(self._secret, broker_nonce, hashlib.sha256).hexdigest()
        self._stream.write(
            encode_frame(
                {"type": "client_proof", "hmac": client_proof, "nonce": client_nonce.hex()}
            )
        )
        self._stream.flush()
        response = read_frame(self._stream)
        expected = hmac.new(self._secret, client_nonce, hashlib.sha256).hexdigest()
        if response.get("type") != "server_proof" or not hmac.compare_digest(
            response.get("hmac", ""), expected
        ):
            raise ProtocolError("broker authentication failed")

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        """Send one request; distinguish pre-write refusal from an unknown effect."""
        if self._stream is None:
            raise ConnectionError("broker is not connected")
        if "id" in arguments or "op" in arguments:
            raise ValueError("id and op are reserved protocol fields")
        request_id = secrets.token_hex(16)
        request = {"id": request_id, "op": operation, **arguments}
        try:
            frame = encode_frame(request)
        except ProtocolError:
            raise
        offset = 0
        try:
            while offset < len(frame):
                count = self._stream.write(frame[offset:])
                if not count:
                    break
                offset += count
            if offset != len(frame):
                raise OSError("broker pipe accepted only part of the request")
            self._stream.flush()
            response = read_frame(self._stream)
        except (OSError, EOFError, ProtocolError) as exc:
            if offset:
                raise EffectOutcomeUnknown(
                    f"broker response lost after request {request_id} was written"
                ) from exc
            raise ConnectionError("broker request was not written") from exc
        if response.get("id") != request_id:
            raise EffectOutcomeUnknown("broker response did not match the request")
        if response.get("ok") is True:
            return response
        error = response.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            raise BrokerRefusal(error["code"], str(error.get("message", "")))
        raise ProtocolError("broker response has an invalid shape")

