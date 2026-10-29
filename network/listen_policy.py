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


def bind_listen_socket(host: str, port: int, family: int = socket.AF_INET) -> socket.socket:
    """Create, apply the platform listen policy to, and bind a listen socket.

    For servers whose own bind path (uvicorn's loop.create_server) sets no
    port-reuse option on Windows: bind through THIS policy instead and hand
    the socket to uvicorn via ``Server.run(sockets=[sock])``. The socket is
    bound but NOT listening -- asyncio puts it on the accept loop itself --
    and is not inheritable by child processes. A failed bind closes the
    socket before re-raising, so nothing leaks a half-bound port.
    """
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        apply_listen_socket_policy(sock)
        sock.bind((host, int(port)))
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(False)
    return sock


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
