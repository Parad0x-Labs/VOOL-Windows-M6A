"""The ephemeral UDP/stream pair must not hinge on one protocol's port counter.

T31's Windows suite (run 41918263493, shard 14) failed every TransportLargePayloadTests case with
"Could not allocate an adjacent UDP/stream port pair after 16 attempts" on a runner where the same
tests passed one train earlier. Windows hands out ephemeral TCP ports in sequence, so 16 attempts
that always let TCP pick its port and then ask for the UDP port just below it probe 16 neighbouring
UDP ports: one held or excluded block of UDP ports (Hyper-V / WinNAT reserve them in blocks) refuses
every attempt. These tests model such a block with a UDP socket that refuses any chosen port, and
its mirror for TCP, and require the pair to come up either way.
"""
from __future__ import annotations

import errno
import socket as _real_socket
import unittest
from unittest.mock import patch

from network.transport import UDPTransportServer
from storage.migrations import run_migrations


def _policy(path: str, default=None):
    overrides = {
        "system.enable_stream_data_plane": True,
        "system.enable_udp_fragmentation": True,
        "system.require_mesh_encryption": False,
        "system.stream_tls_enabled": False,
        "system.stream_tls_certfile": "",
        "system.stream_tls_keyfile": "",
        "system.stream_tls_ca_file": "",
        "system.stream_tls_require_client_cert": False,
        "system.stream_tls_insecure_skip_verify": False,
    }
    return overrides.get(path, default)


class _RefusesChosenPorts(_real_socket.socket):
    """A real socket that refuses a bind to any port other than 0 (a held or excluded block)."""

    def bind(self, address):
        if int(address[1]) != 0:
            raise OSError(errno.EADDRINUSE, "address already in use (modelled held block)")
        return super().bind(address)


class _SocketModule:
    """network.transport's view of the socket module, with one socket type refusing chosen ports."""

    def __init__(self, refused_type: int) -> None:
        self._refused_type = refused_type

    def socket(self, family=_real_socket.AF_INET, type=_real_socket.SOCK_STREAM, *args, **kwargs):
        if type == self._refused_type:
            return _RefusesChosenPorts(family, type, *args, **kwargs)
        return _real_socket.socket(family, type, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(_real_socket, name)


class EphemeralPairAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        run_migrations()

    def _start_with(self, refused_type: int):
        server = UDPTransportServer(host="127.0.0.1", port=0, on_message=lambda *_args: None)
        with patch("network.stun_client.discover_public_endpoint", return_value=None), patch(
            "network.transport.policy_engine.get", side_effect=_policy
        ), patch("network.transport.socket", _SocketModule(refused_type)):
            runtime = server.start()
        self.addCleanup(server.stop)
        return runtime

    def test_a_held_block_of_udp_ports_below_tcp_still_yields_an_adjacent_pair(self) -> None:
        runtime = self._start_with(_real_socket.SOCK_DGRAM)

        self.assertTrue(runtime.running)
        self.assertGreater(runtime.port, 0)
        self.assertEqual(runtime.stream_port, runtime.port + 1)

    def test_a_held_block_of_tcp_ports_above_udp_still_yields_an_adjacent_pair(self) -> None:
        runtime = self._start_with(_real_socket.SOCK_STREAM)

        self.assertTrue(runtime.running)
        self.assertGreater(runtime.port, 0)
        self.assertEqual(runtime.stream_port, runtime.port + 1)


if __name__ == "__main__":
    unittest.main()
