"""Round 5 of the Windows exclusive-listen work: leaks and recovery.

Four failure paths, each pinned red before its fix:

* ``network.transport`` created a UDP socket and only then ran the buffer setup,
  the listen policy and the bind; a policy that failed closed (no
  SO_EXCLUSIVEADDRUSE on this Windows build) or a stream half that refused its
  pre-bound socket left the socket open. Every socket the requested-port and
  ephemeral-pair branches create must close on EVERY failure path.
* The API server and the daemon health site pre-bind their policy sockets and
  only then arm the shutdown cooperators / start the worker thread; a failure
  in that span left the pre-bound sockets holding the port with no server
  coming. The sockets belong to the binder until the server takes them.
* The devnet proof origin server's handler threads count offers, deliveries
  and accepted signatures with plain attribute updates; concurrent requests
  must land exactly once each.
* On Windows a second bind of a port another socket holds SO_EXCLUSIVEADDRUSE
  fails WSAEACCES (errno EACCES / winerror 10013), not EADDRINUSE: UDP bind
  recovery must treat that refusal as the conflict it is -- on Windows only,
  because POSIX EACCES keeps its own meaning there.
"""
from __future__ import annotations

import base64
import errno
import json
import socket
import sys
import threading
import unittest.mock
import urllib.request
from types import SimpleNamespace

import pytest

from apps import vool_api_server
from core.daemon import DaemonConfig
from core.daemon.health import start_health_server
from network import listen_policy, transport
from ops.wallet_devnet_external_proof import _X402Server

#: Arbitrary value standing in for the Windows-only constant (same stand-in as
#: tests/test_listen_policy_fail_closed.py): the policy must pass through the
#: socket module's own constant, whatever its value.
_EXCLUSIVEADDRUSE_SENTINEL = 0x5EED1

_PAY_TO = "DevnetPayme1111111111111111111111111111111"
_AMOUNT = 1_000_000


class _PreboundSocket:
    """A socket stand-in for the Windows pre-bind path: every call lands in a
    shared log, so a test can pin that each bound socket was closed again."""

    def __init__(self, log: list[tuple]) -> None:
        self._log = log

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self._log.append(("setsockopt", level, option, value))

    def bind(self, address: object) -> None:
        self._log.append(("bind", tuple(address)))  # type: ignore[arg-type]

    def set_inheritable(self, inheritable: bool) -> None:
        self._log.append(("set_inheritable", inheritable))

    def close(self) -> None:
        self._log.append(("close",))


def _fake_socket_module() -> tuple[SimpleNamespace, list[_PreboundSocket], list[tuple]]:
    """A socket-module stand-in for listen_policy: getaddrinfo resolves every
    host to one AF_INET address, each created socket records into one log."""
    log: list[tuple] = []
    created: list[_PreboundSocket] = []

    def _socket(*args: object, **kwargs: object) -> _PreboundSocket:
        sock = _PreboundSocket(log)
        created.append(sock)
        return sock

    module = SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        AF_UNSPEC=socket.AF_UNSPEC,
        AI_PASSIVE=socket.AI_PASSIVE,
        IPV6_V6ONLY=socket.IPV6_V6ONLY,
        IPPROTO_IPV6=socket.IPPROTO_IPV6,
        SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        SO_REUSEADDR=socket.SO_REUSEADDR,
        SO_EXCLUSIVEADDRUSE=_EXCLUSIVEADDRUSE_SENTINEL,
        getaddrinfo=lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, int(port)))
        ],
        has_ipv6=True,
        socket=_socket,
    )
    return module, created, log


def _windows_listen_policy(monkeypatch) -> tuple[list[_PreboundSocket], list[tuple]]:
    fake_socket, created, log = _fake_socket_module()
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    return created, log


# --- (a) every UDP socket closes on every failure path --------------------------------------------


def test_fixed_port_udp_socket_closes_when_the_listen_policy_fails_closed(monkeypatch):
    """The policy runs on a socket that already exists. When it fails closed
    (a Windows build without SO_EXCLUSIVEADDRUSE must never hand out a listen
    socket), that socket must be closed again, never leaked bound-less and
    option-less."""
    leaked_socket = unittest.mock.Mock()
    monkeypatch.setattr(socket, "socket", unittest.mock.Mock(return_value=leaked_socket))

    def refusing_policy(_sock):
        raise OSError("SO_EXCLUSIVEADDRUSE is unavailable on this Windows build")

    monkeypatch.setattr(transport, "apply_udp_listen_socket_policy", refusing_policy)

    server = transport.UDPTransportServer(host="127.0.0.1", port=49152)
    with pytest.raises(OSError, match="SO_EXCLUSIVEADDRUSE is unavailable"):
        server.start()

    leaked_socket.bind.assert_not_called()
    leaked_socket.close.assert_called_once()


def test_ephemeral_pair_closes_both_sockets_when_the_stream_half_fails_unexpectedly(monkeypatch):
    """_bind_ephemeral_pair catches only OSError/OverflowError today: a stream
    half that fails any other way (here: it refuses the pre-bound socket) must
    still close BOTH sockets of the attempt, not leak them through the raise."""
    stream_sock = unittest.mock.Mock()
    stream_sock.getsockname.return_value = ("127.0.0.1", 41251)
    udp_sock = unittest.mock.Mock()
    monkeypatch.setattr(socket, "socket", unittest.mock.Mock(side_effect=[stream_sock, udp_sock]))

    class _RefusingStreamServer:
        def __init__(self, *, host, port, on_frame, tls_config):
            pass

        def start(self, prebound_socket=None):
            raise ValueError("stream transport refused its pre-bound socket")

    monkeypatch.setattr(transport, "StreamTransportServer", _RefusingStreamServer)

    server = transport.UDPTransportServer(host="127.0.0.1", port=0)
    with pytest.raises(ValueError, match="refused its pre-bound socket"):
        server.start()

    stream_sock.close.assert_called_once()
    udp_sock.close.assert_called_once()


# --- (b) the pre-bind spans: a failed boot never holds the port ------------------------------------


def test_health_site_closes_prebound_sockets_when_the_worker_never_starts(monkeypatch):
    """Between pre_bind_listen_sockets and the worker thread's start, the
    pre-bound sockets are THIS thread's. A worker that cannot start (no new
    thread) must close them before the error surfaces, or the port stays held
    with no server ever coming."""
    created, log = _windows_listen_policy(monkeypatch)
    daemon = unittest.mock.Mock(
        config=DaemonConfig(
            bind_host="127.0.0.1",
            bind_port=49152,
            health_bind_host="127.0.0.1",
            health_bind_port=19090,
            health_auth_token=None,
        )
    )

    with (
        unittest.mock.patch("uvicorn.Server"),
        unittest.mock.patch("uvicorn.Config"),
        unittest.mock.patch("threading.Thread", side_effect=RuntimeError("can't start new thread")),
    ):
        with pytest.raises(RuntimeError, match="can't start new thread"):
            start_health_server(daemon)

    assert created, "the Windows pre-bind path must have bound the policy sockets"
    assert log.count(("close",)) == len(created), (
        f"every pre-bound socket must close when the worker never starts; "
        f"{log.count(('close',))} closed of {len(created)} bound"
    )


def test_api_server_closes_prebound_sockets_when_the_parent_death_watch_cannot_start(monkeypatch):
    """The API server's span between the pre-bind and run(): the parent-death
    watch thread (armed through VOOL_OWNED_BY_WINDOW_PID) and the SIGTERM shim
    run there. A failure in the span must close the pre-bound sockets and never
    reach server.run()."""
    created, log = _windows_listen_policy(monkeypatch)
    monkeypatch.setenv("VOOL_OWNED_BY_WINDOW_PID", "4242")
    server = unittest.mock.Mock()

    with unittest.mock.patch("threading.Thread", side_effect=RuntimeError("can't start new thread")):
        with pytest.raises(RuntimeError, match="can't start new thread"):
            vool_api_server._serve_with_exclusive_listen_sockets(server, host="127.0.0.1", port=8123)

    assert created, "the Windows pre-bind path must have bound the policy sockets"
    assert log.count(("close",)) == len(created), (
        f"every pre-bound socket must close when the shutdown cooperators fail; "
        f"{log.count(('close',))} closed of {len(created)} bound"
    )
    server.run.assert_not_called()


# --- (c) the proof origin's counters are exact under concurrency -----------------------------------


def _paid_header(signature: str) -> str:
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "payload": {"signature": signature, "payTo": _PAY_TO, "amount": str(_AMOUNT)},
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _devnet_transaction(pay_to: str, amount: int) -> dict:
    """The transaction shape _verify accepts: chain meta with no error whose
    pay_to account really moved `amount` lamports."""
    return {
        "meta": {"err": None, "preBalances": [10_000_000, 0], "postBalances": [10_000_000 - amount, amount]},
        "transaction": {"message": {"accountKeys": ["PAYER", pay_to]}},
    }


def test_the_same_signature_presented_concurrently_is_accepted_once(monkeypatch):
    """The x402 origin is served by ExclusiveThreadingHTTPServer: one handler
    thread per request, all mutating outer.deliveries and
    outer.accepted_signatures. The production flow PRESENTS THE SAME SIGNATURE
    more than once (the repeat fetch and the replay retry both re-present it),
    and the membership check and the append are separate lines a thread switch
    can land between: two handler threads that both pass "not in" both append,
    and the proof's exactly-once record then counts two acceptances of one
    payment. The interleaving is forced at the real handler lines: a thread
    parks the instant it is ABOUT to append -- it has already passed the
    membership check -- until a second thread stands at the same line, or the
    park times out (with the bookkeeping as one locked section the second
    thread cannot reach that line at all: it finds the signature already
    accepted). The chain read is the one stand-in; the counter race is under
    test, not devnet."""
    import linecache

    from ops import wallet_devnet_external_proof as proof

    monkeypatch.setattr(proof, "get_transaction", lambda signature, url="": _devnet_transaction(_PAY_TO, _AMOUNT))

    append_line = "outer.accepted_signatures.append(signature)"
    second_thread_at_the_append = threading.Event()
    guard = threading.Lock()
    parked = {"threads": 0}

    def tracer(frame, event, arg):
        if event == "line" and frame.f_code.co_name == "do_GET":
            if linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip() == append_line:
                with guard:
                    parked["threads"] += 1
                    if parked["threads"] >= 2:
                        second_thread_at_the_append.set()
                second_thread_at_the_append.wait(timeout=5.0)
        return tracer

    replayed_signature = "5" * 88
    server = _X402Server(pay_to=_PAY_TO, amount=_AMOUNT)
    url = server.start()
    failures: list[Exception] = []

    def buyer() -> None:
        try:
            request = urllib.request.Request(url, headers={"X-PAYMENT": _paid_header(replayed_signature)})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    failures.append(RuntimeError(f"paid fetch came back {response.status}"))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            failures.append(exc)

    threading.settrace(tracer)
    try:
        buyers = [threading.Thread(target=buyer) for _ in range(2)]
        for thread in buyers:
            thread.start()
        for thread in buyers:
            thread.join(timeout=60)
    finally:
        threading.settrace(None)
        server.stop()

    assert not failures, f"paid fetches failed: {failures}"
    assert server.deliveries == 2, f"two verified payments were delivered, {server.deliveries} counted"
    assert server.accepted_signatures == [replayed_signature], (
        f"one replayed payment was accepted {len(server.accepted_signatures)} times"
    )
    assert server.rejections == [], f"verified payments were rejected: {server.rejections}"


# --- (d) Windows names an exclusive holder's refusal EACCES ---------------------------------------


def _exclusive_holder_refusal() -> OSError:
    """What a second bind of an SO_EXCLUSIVEADDRUSE-held UDP port raises on
    Windows: WSAEACCES, winerror 10013, errno EACCES."""
    error = OSError(errno.EACCES, "An attempt was made to access a socket in a way forbidden by its access permissions")
    error.winerror = 10013
    return error


def test_windows_recovery_names_an_exclusive_holders_eacces_as_the_conflict(monkeypatch):
    monkeypatch.setattr(transport, "_IS_WINDOWS", True, raising=False)
    held_socket = unittest.mock.Mock()
    held_socket.bind.side_effect = _exclusive_holder_refusal()
    monkeypatch.setattr(socket, "socket", unittest.mock.Mock(return_value=held_socket))
    report = unittest.mock.Mock()
    monkeypatch.setattr(transport, "_report_udp_port_conflict", report)

    server = transport.UDPTransportServer(host="127.0.0.1", port=49152)
    with pytest.raises(OSError, match="already in use"):
        server.start()

    held_socket.close.assert_called_once()
    report.assert_called_once_with(49152)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="WSAEACCES is how Windows refuses a second bind of an exclusively held port",
)
def test_a_live_exclusive_udp_holder_is_named_the_conflict_on_windows(monkeypatch):
    """The same contract on real sockets: a UDP port held SO_EXCLUSIVEADDRUSE
    by this process, then the transport asked for it. The holder hunt is the
    one stand-in (it shells out to lsof); the sockets and the refusal are
    real, whatever errno this Windows build hands the second bind. This is the
    real-workflow twin of the refused-bind test above, which is the one shown
    red first."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("127.0.0.1", 0))
    except OSError:
        holder.close()
        pytest.skip("this Windows build refuses the exclusive UDP hold the test needs")
    port = int(holder.getsockname()[1])
    report = unittest.mock.Mock()
    monkeypatch.setattr(transport, "_report_udp_port_conflict", report)

    try:
        server = transport.UDPTransportServer(host="127.0.0.1", port=port)
        with pytest.raises(OSError, match="already in use"):
            server.start()
    finally:
        holder.close()

    report.assert_called_once_with(port)


def test_posix_recovery_leaves_eacces_as_the_bind_error_it_is(monkeypatch):
    """The other half of the contract: on POSIX EACCES is not a conflict
    verdict, so the raw error must surface unchanged and nothing may be
    reported as a conflict. Green before AND after the fix -- it exists to pin
    that the Windows clause above never leaks onto POSIX."""
    monkeypatch.setattr(transport, "_IS_WINDOWS", False, raising=False)
    held_socket = unittest.mock.Mock()
    held_socket.bind.side_effect = _exclusive_holder_refusal()
    monkeypatch.setattr(socket, "socket", unittest.mock.Mock(return_value=held_socket))
    report = unittest.mock.Mock()
    monkeypatch.setattr(transport, "_report_udp_port_conflict", report)

    server = transport.UDPTransportServer(host="127.0.0.1", port=49152)
    # errno EACCES constructs a PermissionError; that identity must survive.
    with pytest.raises(PermissionError) as raised:
        server.start()

    assert "already in use" not in str(raised.value)
    report.assert_not_called()
    held_socket.close.assert_called_once()
