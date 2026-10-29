"""A live VOOL listener's port cannot be double-bound on Windows.

Windows SO_REUSEADDR lets a second socket bind a port a listener already
holds, and the two sockets then split that port's connections between them
(POSIX refuses the second bind of a live port). http.server's
``allow_reuse_address = 1`` and the raw ``setsockopt(SO_REUSEADDR)`` sites
hand any same-machine process that share, so VOOL listeners set
SO_EXCLUSIVEADDRUSE on Windows instead (network/listen_policy.py). Each test
here starts a REAL listener through its production start path, then binds
exactly like a same-machine port thief -- SO_REUSEADDR, same address -- and
pins that the theft fails.
"""
from __future__ import annotations

import socket
import sys
import tempfile

import pytest

from core.web.meet.server import MeetAndGreetServerConfig
from core.web.meet.server import build_server as build_meet_server
from core.web.watch.config import BrainHiveWatchServerConfig
from core.web.watch.server import build_watch_server
from network.stream_transport import StreamTransportServer
from network.transport import UDPTransportServer
from ops.restart_repro.harness import ProviderStub as ReproProviderStub
from ops.served_reality.provider_stub import ProviderStub
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
