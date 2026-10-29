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
