"""One listen-socket policy for every VOOL listener.

POSIX keeps SO_REUSEADDR exactly as each listener sets it today (a listener may
rebind its own port through TIME_WAIT after a restart). Windows SO_REUSEADDR
means something different: a second socket can bind a port a LIVE listener
already holds, and the two sockets then split that port's inbound connections
between them -- so any other process on the same Windows machine that sets
SO_REUSEADDR can take a share of a VOOL listener's connections (measured 29 Oct
2026: two ThreadingHTTPServer(('127.0.0.1', 12462)) in one process both bound).
On Windows VOOL listen sockets set SO_EXCLUSIVEADDRUSE and never SO_REUSEADDR,
so a second bind of a held port fails closed.
"""
from __future__ import annotations

import socket
import sys
from http.server import ThreadingHTTPServer
from typing import Any

_WINDOWS = sys.platform == "win32"


def apply_listen_socket_policy(sock: socket.socket) -> None:
    """Set this platform's port-reuse policy on a listening socket, before bind()."""
    if _WINDOWS:
        if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Unknown state fails closed: never bind a stealable listen port.
            raise OSError("SO_EXCLUSIVEADDRUSE is unavailable on this Windows build")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def apply_udp_listen_socket_policy(sock: socket.socket) -> None:
    """UDP twin of apply_listen_socket_policy, before bind().

    Windows: the same SO_EXCLUSIVEADDRUSE fail-closed path -- a same-user
    SO_REUSEADDR bind of a held UDP port shares the socket's datagrams.
    POSIX: nothing. The UDP binds set no socket option before this policy
    existed and UDP has no TIME_WAIT to reuse through, so SO_REUSEADDR would
    only add the BSD/macOS behaviour this module exists to refuse (a second
    bind of a live port); POSIX behaviour stays byte-for-byte.
    """
    if _WINDOWS:
        apply_listen_socket_policy(sock)


def bind_listen_socket(host: str, port: int) -> list[socket.socket]:
    """Bind one policy listen socket per address getaddrinfo resolves.

    The address set is what asyncio's create_server binds for (host, port):
    ``socket.getaddrinfo(host, port, AF_UNSPEC, SOCK_STREAM, 0, AI_PASSIVE)``,
    one socket per returned address (IPv6 and dual-stack "localhost" included),
    ``IPV6_V6ONLY`` set on AF_INET6 sockets the way create_server sets it, and
    every socket non-inheritable by child processes. If any bind fails, every
    socket bound so far is closed again before the error surfaces, and an
    empty resolution raises: an unknown or half-bound state must never reach
    a caller (fail closed, never a silently unbound "success").

    The sockets are bound but NOT listening -- asyncio puts them on the accept
    loop itself -- and belong together in one ``Server.run(sockets=...)`` call.
    """
    addresses = socket.getaddrinfo(host, int(port), socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
    sockets: list[socket.socket] = []
    completed = False
    try:
        seen: set[tuple] = set()
        for family, socktype, proto, _canonname, address in addresses:
            if address in seen:
                # getaddrinfo may repeat an address; create_server binds it once.
                continue
            seen.add(address)
            try:
                sock = socket.socket(family, socktype, proto)
            except OSError:
                # A family this platform cannot open (create_server skips these
                # too); the other addresses still bind.
                continue
            try:
                apply_listen_socket_policy(sock)
                if (
                    family == socket.AF_INET6
                    and getattr(socket, "has_ipv6", True)
                    and hasattr(socket, "IPPROTO_IPV6")
                    and hasattr(socket, "IPV6_V6ONLY")
                ):
                    # create_server disables dual-stack on AF_INET6 sockets so one
                    # socket cannot silently listen on both families.
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(address)
            except OSError as bind_error:
                sock.close()
                raise OSError(
                    bind_error.errno,
                    f"error while attempting to bind on address {address!r}: {bind_error}",
                ) from None
            except BaseException:
                sock.close()
                raise
            sock.set_inheritable(False)
            sockets.append(sock)
        if not sockets:
            raise OSError(f"getaddrinfo({host!r}) bound no address for port {int(port)}")
        completed = True
    finally:
        if not completed:
            for sock in sockets:
                sock.close()
    return sockets


def pre_bind_listen_sockets(host: str, port: int) -> list[socket.socket]:
    """[] on POSIX; on Windows the pre-bound policy sockets for a uvicorn Server.

    POSIX keeps uvicorn's own bind path exactly as it was: loop.create_server
    binds every address getaddrinfo returns, so dual-stack "localhost" and
    IPv6 binds stay unchanged. Windows pre-binds through bind_listen_socket
    instead, because create_server sets no port-reuse option there and any
    same-user SO_REUSEADDR process could take a share of the live port.
    """
    if not _WINDOWS:
        return []
    return bind_listen_socket(host, int(port))


def close_listen_sockets(listen_sockets: list[socket.socket]) -> None:
    """Close every pre-bound policy socket.

    The one named operation for giving the sockets back, used both after a
    serving run and on a span that failed BEFORE the server took them: closing
    a socket the server already closed is a no-op, so a failed boot (which owns
    the sockets until run() takes them) never leaves the port held.
    """
    for sock in listen_sockets:
        sock.close()


def run_server_on_listen_sockets(server: Any, listen_sockets: list[socket.socket]) -> None:
    """Run a uvicorn Server, then close every pre-bound socket, however run() ends.

    An empty list is the POSIX path: ``server.run()`` with no ``sockets=``,
    exactly the call the sites made before this policy existed. With sockets,
    uvicorn owns them only while it serves; once run() returns or raises (a
    failed lifespan startup sys.exits before uvicorn takes them), every
    pre-bound socket is closed so nothing keeps holding the port. Closing a
    socket uvicorn already closed does nothing.
    """
    if not listen_sockets:
        server.run()
        return
    try:
        server.run(sockets=listen_sockets)
    finally:
        close_listen_sockets(listen_sockets)


def run_server_with_listen_policy(server: Any, host: str, port: int) -> None:
    """One-line uvicorn launch through this policy (see pre_bind_listen_sockets)."""
    run_server_on_listen_sockets(server, pre_bind_listen_sockets(host, int(port)))


class ExclusiveListenMixin:
    """socketserver mixin: exclusive listen ports on Windows, reuse on POSIX.

    HTTPServer/ThreadingHTTPServer set SO_REUSEADDR whenever allow_reuse_address
    is truthy (socketserver.TCPServer.server_bind), which on Windows lets any
    second socket bind this server's live port and take a share of its
    connections. POSIX behaviour is unchanged: allow_reuse_address stays truthy
    and server_bind sets SO_REUSEADDR exactly as before.
    """

    allow_reuse_address = not _WINDOWS

    def server_bind(self) -> None:
        if _WINDOWS:
            apply_listen_socket_policy(self.socket)
        super().server_bind()


class ExclusiveThreadingHTTPServer(ExclusiveListenMixin, ThreadingHTTPServer):
    """Drop-in ThreadingHTTPServer with the platform listen policy."""
