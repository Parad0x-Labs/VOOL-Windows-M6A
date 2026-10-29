"""The Windows listen policy's fail-closed branch, runnable on POSIX.

Linux/macOS CI never executes the real Windows bind path, so these tests
monkeypatch ``listen_policy._WINDOWS`` and drive the policy against a fake
socket module: the option a Windows holder must set is pinned by value and
ORDER (an option applied after bind() protects nothing), a Windows build
without SO_EXCLUSIVEADDRUSE must refuse to hand out a listen socket at all
(fail closed -- never a stealable port), and the POSIX branches stay exactly
what they were. The ops listener stubs are pinned to the same one policy
through the mixin, so there is one fail-closed path, not four.
"""
from __future__ import annotations

import json
import socket
import unittest.mock
from types import SimpleNamespace

import pytest

from core.daemon import DaemonConfig
from core.daemon.health import start_health_server
from network import listen_policy
from network.listen_policy import ExclusiveListenMixin
from ops.restart_repro.harness import _QuietServer
from ops.served_reality.provider_stub import _QuietHTTPServer
import registry_service.cli as registry_cli

#: Arbitrary value standing in for the Windows-only constant. The policy must
#: pass through exactly the socket module's own constant, whatever its value,
#: so the test defines it rather than depending on a platform header.
_EXCLUSIVEADDRUSE_SENTINEL = 0x5EED1


class _RecordingSocket:
    def __init__(self, calls: list[tuple]) -> None:
        self._calls = calls

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self._calls.append(("setsockopt", level, option, value))

    def bind(self, address: object) -> None:
        self._calls.append(("bind", address))

    def set_inheritable(self, inheritable: bool) -> None:
        self._calls.append(("set_inheritable", inheritable))

    def close(self) -> None:
        self._calls.append(("close",))


def _fake_socket_module(*, with_exclusiveaddruse: bool) -> tuple[SimpleNamespace, list[tuple]]:
    """A stand-in for ``socket`` whose SO_EXCLUSIVEADDRUSE can be deleted,
    plus the call log of the socket it hands out."""
    calls: list[tuple] = []
    attributes: dict[str, object] = {
        "SOL_SOCKET": socket.SOL_SOCKET,
        "SO_REUSEADDR": socket.SO_REUSEADDR,
        "SOCK_STREAM": socket.SOCK_STREAM,
        "socket": lambda *args, **kwargs: _RecordingSocket(calls),
    }
    if with_exclusiveaddruse:
        attributes["SO_EXCLUSIVEADDRUSE"] = _EXCLUSIVEADDRUSE_SENTINEL
    return SimpleNamespace(**attributes), calls


def test_windows_bind_listen_socket_sets_exclusiveaddruse_before_bind(monkeypatch):
    fake_socket, calls = _fake_socket_module(with_exclusiveaddruse=True)
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    listen_policy.bind_listen_socket("127.0.0.1", 8123)

    # Exact call sequence: the option must be on the socket BEFORE the bind
    # (set after bind it protects nothing), and the socket must not be leaked
    # inheritable to child processes.
    assert calls == [
        ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1),
        ("bind", ("127.0.0.1", 8123)),
        ("set_inheritable", False),
    ]


def test_windows_bind_listen_socket_fails_closed_without_exclusiveaddruse(monkeypatch):
    fake_socket, calls = _fake_socket_module(with_exclusiveaddruse=False)
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    # Unknown state fails closed: a Windows build without the constant must
    # raise, never bind a stealable port.
    with pytest.raises(OSError):
        listen_policy.bind_listen_socket("127.0.0.1", 8123)

    assert ("bind", ("127.0.0.1", 8123)) not in calls
    assert ("close",) in calls


def test_posix_bind_listen_socket_keeps_reuseaddr_and_binds(monkeypatch):
    fake_socket, calls = _fake_socket_module(with_exclusiveaddruse=False)
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)

    listen_policy.bind_listen_socket("127.0.0.1", 8123)

    # POSIX behaviour byte-for-byte: SO_REUSEADDR (what uvicorn's own bind and
    # socketserver set on POSIX), the same address, no exclusive option.
    assert calls == [
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", 8123)),
        ("set_inheritable", False),
    ]


def test_windows_udp_listen_policy_sets_exclusiveaddruse(monkeypatch):
    fake_socket, calls = _fake_socket_module(with_exclusiveaddruse=True)
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    listen_policy.apply_udp_listen_socket_policy(_RecordingSocket(calls))

    assert calls == [("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1)]


def test_posix_udp_listen_policy_sets_no_option(monkeypatch):
    fake_socket, calls = _fake_socket_module(with_exclusiveaddruse=True)
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)

    listen_policy.apply_udp_listen_socket_policy(_RecordingSocket(calls))

    # The UDP binds set no socket option on POSIX before the fix and must not
    # after: UDP has no TIME_WAIT to reuse through, and BSD SO_REUSEADDR on UDP
    # permits a second bind of a live port -- the exact share this refuses.
    assert calls == []


def test_ops_listener_stubs_share_the_one_listen_policy():
    # One policy, one fail-closed path: the ops stubs mix in
    # ExclusiveListenMixin instead of duplicating the setsockopt themselves.
    assert ExclusiveListenMixin in _QuietServer.__mro__
    assert ExclusiveListenMixin in _QuietHTTPServer.__mro__


# ---------------------------------------------------------------------------
# The getaddrinfo-driven bind path: every resolved address is bound, the way
# asyncio's create_server binds, and any failure closes everything bound so far.


class _ResolvableSocket:
    """A fake socket for the multi-address bind path: every socket call lands
    in this socket's own log, and a bind to an address in ``refuse_binds``
    raises like a held port."""

    def __init__(self, refuse_binds: frozenset[tuple] = frozenset()) -> None:
        self.calls: list[tuple] = []
        self._refuse_binds = refuse_binds

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address: object) -> None:
        address = tuple(address)
        if address in self._refuse_binds:
            raise OSError(10048, "the port is already held")
        self.calls.append(("bind", address))

    def set_inheritable(self, inheritable: bool) -> None:
        self.calls.append(("set_inheritable", inheritable))

    def close(self) -> None:
        self.calls.append(("close",))


def _resolving_socket_module(
    resolved: list[tuple[int, tuple]],
    *,
    with_exclusiveaddruse: bool = True,
    refuse_binds: frozenset[tuple] = frozenset(),
) -> tuple[SimpleNamespace, list[_ResolvableSocket], list[tuple[tuple, dict]]]:
    """A socket-module stand-in whose getaddrinfo returns ``resolved``
    ((family, sockaddr) pairs, in order). Each socket it creates keeps its own
    call log and refuses binds to addresses in ``refuse_binds``."""
    created: list[_ResolvableSocket] = []
    getaddrinfo_calls: list[tuple[tuple, dict]] = []

    def _getaddrinfo(host, port, *args, **kwargs):
        getaddrinfo_calls.append(((host, port) + tuple(args), kwargs))
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr) for family, sockaddr in resolved]

    def _socket(*args, **kwargs):
        sock = _ResolvableSocket(refuse_binds)
        created.append(sock)
        return sock

    attributes: dict[str, object] = {
        "AF_INET": socket.AF_INET,
        "AF_INET6": socket.AF_INET6,
        "AF_UNSPEC": socket.AF_UNSPEC,
        "AI_PASSIVE": socket.AI_PASSIVE,
        "IPPROTO_IPV6": socket.IPPROTO_IPV6,
        "IPV6_V6ONLY": socket.IPV6_V6ONLY,
        "SOCK_STREAM": socket.SOCK_STREAM,
        "SOL_SOCKET": socket.SOL_SOCKET,
        "SO_REUSEADDR": socket.SO_REUSEADDR,
        "getaddrinfo": _getaddrinfo,
        "has_ipv6": True,
        "socket": _socket,
    }
    if with_exclusiveaddruse:
        attributes["SO_EXCLUSIVEADDRUSE"] = _EXCLUSIVEADDRUSE_SENTINEL
    return SimpleNamespace(**attributes), created, getaddrinfo_calls


def test_bind_listen_socket_binds_every_resolved_address_with_the_windows_policy(monkeypatch):
    fake_socket, created, getaddrinfo_calls = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 8123, 0, 0)), (socket.AF_INET, ("127.0.0.1", 8123))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    bound = listen_policy.bind_listen_socket("localhost", 8123)

    # The address set comes from getaddrinfo(host, port, ..., AI_PASSIVE) -- the
    # same resolution asyncio's create_server uses -- and EVERY returned address
    # is bound, in resolution order, as its own socket for Server.run(sockets=).
    assert bound == created
    assert len(created) == 2
    assert getaddrinfo_calls[0][0][:2] == ("localhost", 8123)
    assert socket.SOCK_STREAM in getaddrinfo_calls[0][0]
    assert socket.AI_PASSIVE in getaddrinfo_calls[0][0]
    assert created[0].calls == [
        ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1),
        ("setsockopt", socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1),
        ("bind", ("::1", 8123, 0, 0)),
        ("set_inheritable", False),
    ]
    assert created[1].calls == [
        ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1),
        ("bind", ("127.0.0.1", 8123)),
        ("set_inheritable", False),
    ]


def test_bind_listen_socket_closes_every_bound_socket_when_a_later_bind_fails(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET, ("127.0.0.1", 8123)), (socket.AF_INET, ("127.0.0.2", 8123))],
        refuse_binds={("127.0.0.2", 8123)},
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    # A half-bound set is worse than none: the second address's failure must
    # close the FIRST socket too before the error surfaces (fail closed).
    with pytest.raises(OSError):
        listen_policy.bind_listen_socket("localhost", 8123)

    assert created[0].calls == [
        ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1),
        ("bind", ("127.0.0.1", 8123)),
        ("set_inheritable", False),
        ("close",),
    ]
    assert created[1].calls == [
        ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1),
        ("close",),
    ]


def test_bind_listen_socket_fails_closed_when_resolution_returns_no_addresses(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module([])
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    # Unknown state fails closed: a host that resolves to nothing must raise,
    # never hand back a listen set that "succeeds" without binding anything.
    with pytest.raises(OSError):
        listen_policy.bind_listen_socket("nowhere.invalid", 8123)

    assert created == []


def test_bind_listen_socket_matches_posix_create_server_options(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 8123, 0, 0)), (socket.AF_INET, ("127.0.0.1", 8123))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)

    listen_policy.bind_listen_socket("localhost", 8123)

    # POSIX options are exactly what uvicorn's loop.create_server sets there:
    # SO_REUSEADDR on every socket, IPV6_V6ONLY on AF_INET6 (no dual-stack
    # surprise), and no exclusive option anywhere.
    assert created[0].calls == [
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("setsockopt", socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1),
        ("bind", ("::1", 8123, 0, 0)),
        ("set_inheritable", False),
    ]
    assert created[1].calls == [
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", 8123)),
        ("set_inheritable", False),
    ]


def test_pre_bind_listen_sockets_is_a_noop_on_posix(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module([(socket.AF_INET, ("127.0.0.1", 8123))])
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)

    # POSIX keeps uvicorn's own bind path byte-for-byte: nothing is pre-bound.
    assert listen_policy.pre_bind_listen_sockets("127.0.0.1", 8123) == []
    assert created == []


def test_pre_bind_listen_sockets_binds_through_the_policy_on_windows(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module([(socket.AF_INET, ("127.0.0.1", 8123))])
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)

    prebound = listen_policy.pre_bind_listen_sockets("127.0.0.1", 8123)

    assert prebound == created
    assert created[0].calls[0] == ("setsockopt", socket.SOL_SOCKET, _EXCLUSIVEADDRUSE_SENTINEL, 1)


def test_run_server_on_listen_sockets_runs_bare_without_prebound_sockets(monkeypatch):
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)
    server = unittest.mock.Mock()

    listen_policy.run_server_on_listen_sockets(server, [])

    # The POSIX call is exactly today's: run() with no sockets argument.
    server.run.assert_called_once_with()


def test_run_server_on_listen_sockets_hands_every_socket_to_uvicorn_and_closes_untaken_ones(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 8123, 0, 0)), (socket.AF_INET, ("127.0.0.1", 8123))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    server = unittest.mock.Mock()

    listen_policy.run_server_on_listen_sockets(server, listen_policy.pre_bind_listen_sockets("localhost", 8123))

    server.run.assert_called_once_with(sockets=created)
    # Even a run() that returns without taking the sockets must not leak one.
    assert all(sock.calls[-1] == ("close",) for sock in created)


def test_run_server_on_listen_sockets_closes_prebound_sockets_when_run_raises(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module([(socket.AF_INET, ("127.0.0.1", 8123))])
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    server = unittest.mock.Mock()
    server.run.side_effect = OSError("uvicorn startup refused the sockets")

    with pytest.raises(OSError):
        listen_policy.run_server_on_listen_sockets(server, listen_policy.pre_bind_listen_sockets("127.0.0.1", 8123))

    assert created[0].calls[-1] == ("close",)


def test_run_server_with_listen_policy_keeps_uvicorns_own_bind_on_posix(monkeypatch):
    monkeypatch.setattr(listen_policy, "_WINDOWS", False)
    server = unittest.mock.Mock()

    listen_policy.run_server_with_listen_policy(server, "127.0.0.1", 8123)

    server.run.assert_called_once_with()


def test_run_server_with_listen_policy_hands_every_resolved_address_to_uvicorn(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 8123, 0, 0)), (socket.AF_INET, ("127.0.0.1", 8123))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    server = unittest.mock.Mock()

    listen_policy.run_server_with_listen_policy(server, "localhost", 8123)

    server.run.assert_called_once_with(sockets=created)


def test_health_site_hands_every_resolved_address_to_run_sockets(monkeypatch):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 19090, 0, 0)), (socket.AF_INET, ("127.0.0.1", 19090))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    daemon = unittest.mock.Mock(
        config=DaemonConfig(
            bind_host="127.0.0.1",
            bind_port=49152,
            health_bind_host="localhost",
            health_bind_port=19090,
            health_auth_token=None,
        )
    )

    with unittest.mock.patch("uvicorn.Server") as server_cls, unittest.mock.patch("uvicorn.Config"):
        server_cls.return_value.run = unittest.mock.Mock()
        start_health_server(daemon)
        assert daemon._health_thread is not None
        daemon._health_thread.join(timeout=10.0)

    assert len(created) == 2
    server_cls.return_value.run.assert_called_once_with(sockets=[created[0], created[1]])


def test_registry_serve_hands_every_resolved_address_to_run_sockets(monkeypatch, tmp_path):
    fake_socket, created, _ = _resolving_socket_module(
        [(socket.AF_INET6, ("::1", 8791, 0, 0)), (socket.AF_INET, ("127.0.0.1", 8791))]
    )
    monkeypatch.setattr(listen_policy, "socket", fake_socket)
    monkeypatch.setattr(listen_policy, "_WINDOWS", True)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"entries": []}', encoding="utf-8")
    config_path = tmp_path / "service.json"
    config_path.write_text(json.dumps({"catalog_file": str(catalog), "target": "test"}), encoding="utf-8")

    with (
        unittest.mock.patch.object(registry_cli, "load_service", return_value=tuple(unittest.mock.Mock() for _ in range(3))),
        unittest.mock.patch("uvicorn.Server") as server_cls,
        unittest.mock.patch("uvicorn.Config"),
    ):
        exit_code = registry_cli.main(["serve", "--config", str(config_path), "--port", "8791"])

    assert exit_code == 0
    assert len(created) == 2
    server_cls.return_value.run.assert_called_once_with(sockets=[created[0], created[1]])
