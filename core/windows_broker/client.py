"""Synchronous named-pipe client for the Windows broker."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from typing import Any

from .protocol import (
    BatchNotRestored,
    BatchRestored,
    BrokerRefusal,
    EffectOutcomeUnknown,
    ProtocolError,
    encode_frame,
    read_frame,
)

_ERROR_PIPE_BUSY = 231
_PIPE_BUSY_WAIT_SECONDS = 5.0


def _open_pipe(pipe_name: str) -> Any:
    """Open the pipe, waiting while every instance is taken (the broker offers the next one)."""
    import ctypes

    deadline = time.monotonic() + _PIPE_BUSY_WAIT_SECONDS
    while True:
        try:
            # The stream stays open until close() so the caller can make several calls.
            return open(pipe_name, "r+b", buffering=0)
        except OSError as exc:
            if getattr(exc, "winerror", None) != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                raise
        # Returns when an instance is free, at the deadline, or at once if the pipe is gone; the
        # open above then decides.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitNamedPipeW
        wait.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        wait.restype = ctypes.c_int
        wait(pipe_name, max(1, int((deadline - time.monotonic()) * 1000)))


def _named_pipe_server_pid(stream: Any) -> int:
    """The process id of the server end of a named pipe (GetNamedPipeServerProcessId)."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    server_pid = wintypes.ULONG()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_server_pid = kernel32.GetNamedPipeServerProcessId
    get_server_pid.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    get_server_pid.restype = wintypes.BOOL
    if not get_server_pid(handle, ctypes.byref(server_pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(server_pid.value)


class BrokerClient:
    """A single-request-at-a-time client for the broker's pipe.

    connect() opens the pipe, checks that its server is the broker process, and authenticates
    both ways. ``opener`` and ``server_pid`` replace the Windows pipe open and the server process
    check; tests use them to drive the client over a socket.
    """

    def __init__(
        self,
        pipe_name: str,
        secret: bytes,
        broker_pid: int,
        *,
        opener: Callable[[str], Any] | None = None,
        server_pid: Callable[[Any], int] | None = None,
    ) -> None:
        if opener is None and os.name != "nt":
            raise OSError("the Windows broker is available only on Windows")
        if not pipe_name.startswith(r"\\.\pipe\vool-broker-"):
            raise ValueError("invalid broker pipe name")
        if not isinstance(secret, bytes) or len(secret) != 32:
            raise ValueError("broker secret must be 256 bits of bytes")
        if broker_pid <= 0:
            raise ValueError("invalid broker process id")
        self._pipe_name = pipe_name
        self._secret = secret
        self._broker_pid = broker_pid
        self._open = opener or _open_pipe
        self._server_pid = server_pid or _named_pipe_server_pid
        self._stream: Any | None = None

    def connect(self) -> None:
        """Open the broker pipe, check its server process, and authenticate both ways."""
        if self._stream is not None:
            return
        try:
            self._stream = self._open(self._pipe_name)
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
        # Checked before anything derived from the secret is sent.
        if self._server_pid(self._stream) != self._broker_pid:
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
        proof = response.get("hmac")
        if (
            response.get("type") != "server_proof"
            or not isinstance(proof, str)
            or not proof.isascii()
            or not hmac.compare_digest(proof, expected)
        ):
            raise ProtocolError("broker authentication failed")

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        """Send one request and return its ok reply.

        Raises ConnectionError when nothing was written, BrokerRefusal (BatchRestored for a batch
        that was put back) when the broker refused it with nothing left changed, and
        EffectOutcomeUnknown (BatchNotRestored for a batch that could not all be put back) when
        the request may have had an effect. After EffectOutcomeUnknown the connection is closed.
        """
        if self._stream is None:
            raise ConnectionError("broker is not connected")
        if "id" in arguments or "op" in arguments:
            raise ValueError("id and op are reserved protocol fields")
        request_id = secrets.token_hex(16)
        request = {"id": request_id, "op": operation, **arguments}
        frame = encode_frame(request)
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
            # Whatever was or was not read, the stream is no longer in step with the broker.
            self.close()
            if offset:
                raise EffectOutcomeUnknown(
                    f"broker response lost after request {request_id} was written"
                ) from exc
            raise ConnectionError("broker request was not written") from exc
        try:
            return self._result(request_id, response)
        except EffectOutcomeUnknown:
            self.close()
            raise

    @staticmethod
    def _result(request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        # A reply for another id (a too_large answered with a null id among them) says nothing
        # about whether this request ran.
        if response.get("id") != request_id:
            raise EffectOutcomeUnknown("broker response did not match the request")
        if response.get("ok") is True:
            return response
        error = response.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise EffectOutcomeUnknown("broker response has an invalid shape")
        code = error["code"]
        message = str(error.get("message", ""))
        batch = response.get("batch")
        if batch is not None:
            if not isinstance(batch, dict):
                raise EffectOutcomeUnknown("broker response has an invalid shape")
            if batch.get("restored") is True and code != "restore_incomplete":
                raise BatchRestored(code, message, batch)
            cause = error.get("cause")
            raise BatchNotRestored(code, message, cause if isinstance(cause, str) else None, batch)
        if code == "restore_incomplete":
            raise BatchNotRestored(code, message, None, {})
        if code == "reply_unencodable":
            # The request may have run; only its reply was lost.
            raise EffectOutcomeUnknown(f"{code}: {message}" if message else code)
        raise BrokerRefusal(code, message)
