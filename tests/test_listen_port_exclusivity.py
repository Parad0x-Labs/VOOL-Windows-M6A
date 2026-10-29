"""A live VOOL listener's port cannot be double-bound on Windows.

Windows SO_REUSEADDR lets a second socket bind a port a listener already
holds, and the two sockets then split that port's connections between them
(POSIX refuses the second bind of a live port). http.server's
``allow_reuse_address = 1``, the raw ``setsockopt(SO_REUSEADDR)`` sites, and
asyncio's bind path (uvicorn's ``loop.create_server`` sets no port-reuse
option on Windows) hand any same-machine process that share; for UDP sockets
the share is the DATAGRAMS. So every VOOL listener sets SO_EXCLUSIVEADDRUSE
on Windows instead (network/listen_policy.py). Each test here starts a REAL
listener through its production start path -- including the API server in its
own process and the daemon health server -- then binds exactly like a
same-machine port thief -- SO_REUSEADDR, same address -- and pins that the
theft fails.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from core.daemon import DaemonConfig
from core.daemon.health import start_health_server
from core.web.meet.server import MeetAndGreetServerConfig
from core.web.meet.server import build_server as build_meet_server
from core.web.watch.config import BrainHiveWatchServerConfig
from core.web.watch.server import build_watch_server
from network.stream_transport import StreamTransportServer
from network.transport import UDPTransportServer
from ops.restart_repro.harness import ProviderStub as ReproProviderStub
from ops.served_reality.provider_stub import ProviderStub
from ops.wallet_devnet_external_proof import _X402Server
from relay.bridge_workers.webhook_ingress import WebhookIngressConfig, WebhookIngressServer
from relay.http_mirror_server import HttpMirrorServer, MirrorServerConfig

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows SO_REUSEADDR permits a double bind of a live port; POSIX refuses it by default",
)


def _thief_bind(address: tuple[str, int]) -> None:
    """Bind exactly like a same-machine port thief: SO_REUSEADDR, same address."""
    thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        thief.bind(address)
    finally:
        thief.close()


def _thief_bind_udp(address: tuple[str, int]) -> None:
    """The datagram twin of _thief_bind: on Windows a second SO_REUSEADDR UDP
    socket binds a held port and the two sockets then SHARE its datagrams."""
    thief = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        thief.bind(address)
    finally:
        thief.close()


def _assert_socket_holds_exclusiveaddruse(sock: socket.socket, what: str) -> None:
    """Pin the LISTEN CONTRACT directly on the live socket: SO_EXCLUSIVEADDRUSE
    must be set. The bind outcome alone is not enough -- whether a REUSEADDR
    thief can steal an option-less port depends on the Windows build (this
    build refuses it; older builds allow it), so the only build-independent
    pin is the option itself."""
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1, (
        f"{what} listen socket does not hold SO_EXCLUSIVEADDRUSE"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def _stream_tls_disabled():
    """Pin the policy baseline the way the suite's conftest does: the shipped
    default enables stream TLS with empty certfiles, which refuses to start a
    stream half before any bind. Everything else stays as loaded."""
    from core import policy_engine

    previous_cache = getattr(policy_engine, "_POLICY_CACHE", None)
    base = dict(policy_engine.load())
    system = dict(base.get("system") or {})
    system["stream_tls_enabled"] = False
    base["system"] = system
    policy_engine._POLICY_CACHE = base
    try:
        yield
    finally:
        policy_engine._POLICY_CACHE = previous_cache


def test_a_live_meet_listener_port_cannot_be_double_bound():
    server = build_meet_server(MeetAndGreetServerConfig(host="127.0.0.1", port=0))
    try:
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", int(server.server_address[1])))
    finally:
        server.server_close()


def test_a_live_watch_listener_port_cannot_be_double_bound():
    # Inert dependency lambdas: this pins the BIND (the production default
    # server_factory), not any upstream fetch.
    server = build_watch_server(
        BrainHiveWatchServerConfig(host="127.0.0.1", port=0),
        workstation_version="test",
        validate_tls_config=lambda cfg: None,
        requires_public_tls=lambda host: False,
        watch_tls_enabled=lambda cfg: False,
        fetch_dashboard_from_upstreams=lambda *a, **k: {},
        fetch_topic_from_upstreams=lambda *a, **k: {},
        fetch_topic_posts_from_upstreams=lambda *a, **k: [],
        proxy_voolbook_get=lambda *a, **k: {},
        http_get_json=lambda *a, **k: {},
        normalize_base_url=lambda url: url,
        ssl_context_for_url=lambda *a, **k: None,
        build_tls_context=lambda cfg: None,
    )
    try:
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", int(server.server_address[1])))
    finally:
        server.server_close()


def test_a_live_mirror_listener_port_cannot_be_double_bound():
    with tempfile.TemporaryDirectory(prefix="vool-mirror-") as data_dir:
        server = HttpMirrorServer(MirrorServerConfig(host="127.0.0.1", port=0, data_dir=data_dir))
        server.start()
        try:
            assert server.httpd is not None
            with pytest.raises(OSError):
                _thief_bind(("127.0.0.1", int(server.httpd.server_address[1])))
        finally:
            server.stop()


def test_a_live_webhook_ingress_listener_port_cannot_be_double_bound():
    server = WebhookIngressServer(WebhookIngressConfig(host="127.0.0.1", port=0))
    server.start()
    try:
        assert server.httpd is not None
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", int(server.httpd.server_address[1])))
    finally:
        server.stop()


def test_a_live_stream_transport_listener_port_cannot_be_double_bound():
    server = StreamTransportServer(host="127.0.0.1", port=0)
    endpoint = server.start()
    try:
        with pytest.raises(OSError):
            _thief_bind((endpoint.host, int(endpoint.port)))
    finally:
        server.stop()


def test_a_live_mesh_transport_stream_port_cannot_be_double_bound():
    from core import policy_engine

    # The shipped default policy enables stream TLS with empty certfiles (an
    # operator is meant to configure certs), which refuses to start the stream
    # half before any bind. Disable exactly that key the same way the suite's
    # conftest pins its policy baselines; everything else stays as loaded.
    previous_cache = getattr(policy_engine, "_POLICY_CACHE", None)
    base = dict(policy_engine.load())
    system = dict(base.get("system") or {})
    system["stream_tls_enabled"] = False
    base["system"] = system
    policy_engine._POLICY_CACHE = base
    try:
        transport = UDPTransportServer(host="127.0.0.1", port=0)
        runtime = transport.start()
        try:
            assert runtime.stream_port is not None
            with pytest.raises(OSError):
                _thief_bind(("127.0.0.1", int(runtime.stream_port)))
        finally:
            transport.stop()
    finally:
        policy_engine._POLICY_CACHE = previous_cache


def test_a_live_served_reality_stub_port_cannot_be_double_bound():
    stub = ProviderStub()
    port = stub.start()
    try:
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", int(port)))
    finally:
        stub.stop()


def test_a_live_restart_repro_stub_port_cannot_be_double_bound():
    stub = ReproProviderStub(bind_host="127.0.0.1")
    port = stub.start()
    try:
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", int(port)))
    finally:
        stub.stop()


def test_a_live_api_server_port_cannot_be_double_bound(tmp_path):
    """The MAIN API listener. apps.vool_api_server's main() in its own process,
    fresh home, gated on its own /healthz -- the production start path. uvicorn
    binds through asyncio's create_server, which sets no port-reuse option on
    Windows, so without network.listen_policy.bind_listen_socket the port is
    stealable by any same-user process."""
    repo_root = Path(__file__).resolve().parent.parent
    port = _free_port()
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["VOOL_HOME"] = str(tmp_path / "api-home")
    env["VOOL_SKIP_PROVIDER_PREWARM"] = "1"
    env["VOOL_DISABLE_COMPUTE_MODE"] = "1"
    log_path = tmp_path / "api-server.log"
    with log_path.open("wb") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "apps.vool_api_server", "--port", str(port), "--bind", "127.0.0.1"],
            cwd=str(repo_root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 240.0
            healthy = False
            while time.monotonic() < deadline:
                tail = log_path.read_text("utf-8", "replace").splitlines()[-20:]
                assert proc.poll() is None, "API server exited during boot:\n" + "\n".join(tail)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                        if response.status == 200:
                            healthy = True
                            break
                except OSError:
                    time.sleep(1.0)
            assert healthy, "API server never became healthy"
            with pytest.raises(OSError):
                _thief_bind(("127.0.0.1", port))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


class _HealthDaemon:
    """The daemon state core.daemon.health.start_health_server reads: a real
    DaemonConfig plus the snapshot hook (the shape tests/test_daemon_health.py
    pins). The listener under test -- the health server itself -- is real."""

    def __init__(self, *, host: str, port: int) -> None:
        self.config = DaemonConfig(
            bind_host="127.0.0.1",
            bind_port=49152,
            health_bind_host=host,
            health_bind_port=port,
            health_auth_token=None,
        )
        self._health_server = None
        self._health_thread = None

    def _health_snapshot(self) -> dict[str, object]:
        return {"service": "daemon", "status": "ok"}


def test_a_live_health_server_port_cannot_be_double_bound():
    """The daemon health listener (core/daemon/health.py start_health_server,
    the same entrypoint core.agent_runtime.daemon calls): started for real in
    its own thread, then the theft must fail."""
    daemon = _HealthDaemon(host="127.0.0.1", port=_free_port())
    start_health_server(daemon)
    server = daemon._health_server
    thread = daemon._health_thread
    assert server is not None and thread is not None
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not server.started:
            if not thread.is_alive():
                break
            time.sleep(0.05)
        assert server.started, "health server never started listening"
        # The listen contract on the live socket, not just the bind outcome.
        assert server.servers, "health server is not holding any listener socket"
        _assert_socket_holds_exclusiveaddruse(server.servers[0].sockets[0], "health server")
        with pytest.raises(OSError):
            _thief_bind((daemon.config.health_bind_host, int(daemon.config.health_bind_port)))
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_a_live_mesh_transport_udp_port_cannot_be_double_bound():
    # The ephemeral-pair path (port=0): the UDP half binds the port ADJACENT to
    # the stream half (network/transport.py _bind_ephemeral_pair). A thief that
    # double-binds it shares the transport's datagrams.
    with _stream_tls_disabled():
        transport = UDPTransportServer(host="127.0.0.1", port=0)
        runtime = transport.start()
        try:
            assert runtime.port is not None
            assert transport._sock is not None
            _assert_socket_holds_exclusiveaddruse(transport._sock, "ephemeral-pair UDP transport")
            with pytest.raises(OSError):
                _thief_bind_udp(("127.0.0.1", int(runtime.port)))
        finally:
            transport.stop()


def test_a_live_fixed_port_udp_transport_port_cannot_be_double_bound():
    # The fixed-port path (network/transport.py start, requested_port > 0):
    # the UDP socket binds exactly (host, requested_port).
    with _stream_tls_disabled():
        transport = UDPTransportServer(host="127.0.0.1", port=_free_port())
        runtime = transport.start()
        try:
            assert runtime.port is not None
            assert transport._sock is not None
            _assert_socket_holds_exclusiveaddruse(transport._sock, "fixed-port UDP transport")
            with pytest.raises(OSError):
                _thief_bind_udp(("127.0.0.1", int(runtime.port)))
        finally:
            transport.stop()


def test_a_live_wallet_devnet_x402_server_port_cannot_be_double_bound():
    # ops/wallet_devnet_external_proof.py's x402 origin server, started through
    # its real start() (the HTTP hop never leaves loopback; no payment is
    # presented, so nothing touches devnet).
    server = _X402Server(pay_to="DevnetPayme1111111111111111111111111111111", amount=1)
    url = server.start()
    try:
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        with pytest.raises(OSError):
            _thief_bind(("127.0.0.1", port))
    finally:
        server.stop()
