"""The client's own protocol logic against a scripted broker over a socket (runs on any OS)."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import threading
from collections.abc import Callable
from typing import Any

import pytest

from core.windows_broker.client import BrokerClient
from core.windows_broker.protocol import (
    BatchNotRestored,
    BatchRestored,
    BrokerRefusal,
    EffectOutcomeUnknown,
    encode_frame,
    read_frame,
)

PIPE = r"\\.\pipe\vool-broker-test"
BROKER_PID = 4242

Script = Callable[[Any, dict[str, Any]], None]


class FakeBroker:
    """One connection: the handshake, as the broker does it (or broken on purpose), then a script."""

    def __init__(self, secret: bytes, script: Script | None = None, *, proof: str | None = None) -> None:
        self.secret = secret
        self.script = script
        self.proof = proof
        self.received: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self.client_sock, self.server_sock = socket.socketpair()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def open(self, _name: str) -> Any:
        return self.client_sock.makefile("rwb", buffering=0)

    def _serve(self) -> None:
        stream = self.server_sock.makefile("rwb", buffering=0)
        try:
            nonce = os.urandom(32)
            stream.write(encode_frame({"type": "challenge", "nonce": nonce.hex()}))
            proof = read_frame(stream)
            self.received.append(proof)
            client_nonce = bytes.fromhex(proof["nonce"])
            assert hmac.compare_digest(
                proof["hmac"], hmac.new(self.secret, nonce, hashlib.sha256).hexdigest()
            )
            server_proof = (
                self.proof
                if self.proof is not None
                else hmac.new(self.secret, client_nonce, hashlib.sha256).hexdigest()
            )
            stream.write(encode_frame({"type": "server_proof", "hmac": server_proof}))
            if self.script is not None:
                request = read_frame(stream)
                self.received.append(request)
                self.script(stream, request)
        except (EOFError, OSError):
            pass
        except BaseException as exc:  # surfaced by join()
            self.error = exc
        finally:
            stream.close()
            self.server_sock.close()

    def join(self) -> None:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.client_sock.close()
        self.server_sock.close()


@pytest.fixture
def secret() -> bytes:
    return os.urandom(32)


def _client(broker: FakeBroker, secret: bytes, pid: int = BROKER_PID) -> BrokerClient:
    return BrokerClient(PIPE, secret, BROKER_PID, opener=broker.open, server_pid=lambda _s: pid)


def _reply(body: dict[str, Any]) -> Script:
    def script(stream: Any, request: dict[str, Any]) -> None:
        stream.write(encode_frame({"id": request["id"], **body}))

    return script


def _request(broker: FakeBroker, secret: bytes) -> BrokerClient:
    client = _client(broker, secret)
    client.connect()
    return client


def test_authenticates_and_returns_an_ok_reply(secret: bytes) -> None:
    broker = FakeBroker(secret, _reply({"ok": True, "pong": 1}))
    client = _request(broker, secret)
    try:
        assert client.request("ping")["pong"] == 1
    finally:
        client.close()
        broker.join()
    assert broker.received[1]["op"] == "ping"


def test_another_server_process_gets_nothing_derived_from_the_secret(secret: bytes) -> None:
    broker = FakeBroker(secret)
    client = _client(broker, secret, pid=BROKER_PID + 1)
    with pytest.raises(ConnectionError, match="unavailable"):
        client.connect()
    broker.close()
    broker.join()
    assert broker.received == []


@pytest.mark.parametrize("proof", ["0" * 64, "", "é" * 64])
def test_a_wrong_server_proof_fails_authentication(secret: bytes, proof: str) -> None:
    broker = FakeBroker(secret, proof=proof)
    client = _client(broker, secret)
    with pytest.raises(ConnectionError, match="authentication failed"):
        client.connect()
    with pytest.raises(ConnectionError, match="not connected"):
        client.request("ping")
    broker.join()


def test_a_server_proof_that_is_not_text_fails_authentication(secret: bytes) -> None:
    def serve(sock: socket.socket) -> None:
        stream = sock.makefile("rwb", buffering=0)
        stream.write(encode_frame({"type": "challenge", "nonce": os.urandom(32).hex()}))
        read_frame(stream)
        stream.write(encode_frame({"type": "server_proof", "hmac": 7}))
        stream.close()

    client_sock, server_sock = socket.socketpair()
    thread = threading.Thread(target=serve, args=(server_sock,), daemon=True)
    thread.start()
    client = BrokerClient(
        PIPE,
        secret,
        BROKER_PID,
        opener=lambda _n: client_sock.makefile("rwb", buffering=0),
        server_pid=lambda _s: BROKER_PID,
    )
    with pytest.raises(ConnectionError, match="authentication failed"):
        client.connect()
    thread.join(timeout=10)
    client_sock.close()
    server_sock.close()


def test_a_plain_refusal_is_a_broker_refusal(secret: bytes) -> None:
    broker = FakeBroker(secret, _reply({"ok": False, "error": {"code": "not_found", "message": "x"}}))
    client = _request(broker, secret)
    with pytest.raises(BrokerRefusal) as raised:
        client.request("stat", root_id="r", rel="a")
    assert raised.value.code == "not_found"
    assert not isinstance(raised.value, EffectOutcomeUnknown)
    client.close()
    broker.join()


def test_a_batch_put_back_in_full_is_a_refusal_that_keeps_its_report(secret: bytes) -> None:
    batch = {"batch_id": "b" * 32, "ran": 2, "restored": True, "left": []}
    broker = FakeBroker(
        secret,
        _reply({"ok": False, "error": {"code": "destination_exists", "message": "m", "cause": "destination_exists"}, "batch": batch}),
    )
    client = _request(broker, secret)
    with pytest.raises(BatchRestored) as raised:
        client.request("apply_batch", root_id="r", ops=[])
    assert isinstance(raised.value, BrokerRefusal)
    assert raised.value.code == "destination_exists"
    assert raised.value.batch == batch
    client.close()
    broker.join()


def test_a_batch_not_put_back_is_an_unknown_outcome_with_its_report(secret: bytes) -> None:
    batch = {"batch_id": "c" * 32, "ran": 3, "restored": False, "left": ["aside f.txt: in_use"]}
    broker = FakeBroker(
        secret,
        _reply({"ok": False, "error": {"code": "restore_incomplete", "message": "m", "cause": "io_error"}, "batch": batch}),
    )
    client = _request(broker, secret)
    with pytest.raises(BatchNotRestored) as raised:
        client.request("apply_batch", root_id="r", ops=[])
    assert isinstance(raised.value, EffectOutcomeUnknown)
    assert not isinstance(raised.value, BrokerRefusal)
    assert (raised.value.code, raised.value.cause, raised.value.batch) == ("restore_incomplete", "io_error", batch)
    with pytest.raises(ConnectionError, match="not connected"):
        client.request("ping")
    broker.join()


def test_restore_incomplete_from_restore_batch_is_an_unknown_outcome(secret: bytes) -> None:
    batch = {"batch_id": "d" * 32, "ran": None, "restored": False, "left": ["x"]}
    broker = FakeBroker(
        secret,
        _reply({"ok": False, "error": {"code": "restore_incomplete", "message": "m"}, "batch": batch}),
    )
    client = _request(broker, secret)
    with pytest.raises(BatchNotRestored) as raised:
        client.request("restore_batch", batch_id="d" * 32)
    assert raised.value.cause is None and raised.value.batch == batch
    broker.join()


@pytest.mark.parametrize(
    "body",
    [
        {"ok": False, "error": {"code": "reply_unencodable", "message": "m"}},
        {"ok": False, "error": "not an object"},
        {"ok": False, "error": {"code": "io_error"}, "batch": "not an object"},
    ],
    ids=["reply_unencodable", "invalid_error", "invalid_batch"],
)
def test_a_reply_that_hides_the_outcome_is_unknown_and_closes(secret: bytes, body: dict[str, Any]) -> None:
    broker = FakeBroker(secret, _reply(body))
    client = _request(broker, secret)
    with pytest.raises(EffectOutcomeUnknown) as raised:
        client.request("write_file", root_id="r", rel="a", data="", expect="any")
    assert not isinstance(raised.value, BrokerRefusal)
    with pytest.raises(ConnectionError, match="not connected"):
        client.request("ping")
    broker.join()


@pytest.mark.parametrize("reply_id", [None, "someone-else"], ids=["null_id_too_large", "other_id"])
def test_a_reply_for_another_id_is_unknown(secret: bytes, reply_id: str | None) -> None:
    def script(stream: Any, _request: dict[str, Any]) -> None:
        stream.write(encode_frame({"id": reply_id, "ok": False, "error": {"code": "too_large", "message": "m"}}))

    broker = FakeBroker(secret, script)
    client = _request(broker, secret)
    with pytest.raises(EffectOutcomeUnknown, match="did not match"):
        client.request("ping")
    broker.join()


def test_a_connection_lost_after_the_write_is_unknown(secret: bytes) -> None:
    broker = FakeBroker(secret, lambda _stream, _request: None)
    client = _request(broker, secret)
    with pytest.raises(EffectOutcomeUnknown, match="was written"):
        client.request("delete", root_id="r", rel="a")
    broker.join()
    assert broker.received[1]["op"] == "delete"


def test_a_request_that_was_not_written_is_a_connection_error(secret: bytes) -> None:
    broker = FakeBroker(secret, lambda _stream, _request: None)
    client = _request(broker, secret)

    class NoWrite:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def write(self, _data: bytes) -> int:
            raise BrokenPipeError("pipe closed")

        def close(self) -> None:
            self.inner.close()

    # The one way to fail before the write.
    client._stream = NoWrite(client._stream)
    with pytest.raises(ConnectionError, match="not written"):
        client.request("delete", root_id="r", rel="a")
    broker.close()
    broker.join()


def test_the_secret_must_be_bytes() -> None:
    with pytest.raises(ValueError, match="256 bits"):
        BrokerClient(PIPE, "0" * 32, BROKER_PID, opener=lambda _n: None)  # type: ignore[arg-type]
