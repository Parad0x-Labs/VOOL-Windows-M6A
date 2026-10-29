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

import socket
from types import SimpleNamespace

import pytest

from network import listen_policy
from network.listen_policy import ExclusiveListenMixin
from ops.restart_repro.harness import _QuietServer
from ops.served_reality.provider_stub import _QuietHTTPServer

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
