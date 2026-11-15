"""One opt-in for the existing rows that drive cloud paths, so they keep their meaning on Windows.

0.7.1 for Windows sends nothing to a cloud model provider until the owner has stored a key AND
turned cloud on (core/windows_cloud_gate.py). Rows written before that rule drive the OpenRouter
catalog, the chat cloud intents or a BYOK lane (on a mocked wire) in a profile with neither, so on
Windows the gate answers "Cloud models are off..." before the behaviour the row pins is reached
(IM Windows run 42783582031 and IM suite 42794203877, 2 of them because an earlier row had left a
key and cloud consent in the shared session home). The rows listed drive the OpenRouter catalog, a
chat cloud intent, a BYOK lane or the cloud transport (on a mocked wire) to pin behaviour that is not
about the key or the consent: dialects, adapters, deadlines, attribution, pricing and pin authority.

A row whose PREMISE is no stored key, no consent, a fresh install or no opt-in is not listed: on
Windows it checks the new rule instead (see the re-pin helpers at the end of this module), and off
Windows what it always did. Those rows are test_fresh_install_network_defaults
::test_saved_cloud_opt_in_does_not_enable_background_peers (3), test_v050_auto_local_only_mode
::TestNegativeControls::test_the_cloud_transport_works_normally_outside_the_ambient_scope,
test_provider_credential_dispatch::test_missing_or_unreadable_saved_key_never_sends_health_or_completion
(2), test_provider_failure_truthfulness::test_structured_dispatch_refuses_before_wire_with_recovery_action
and ::test_unreadable_vault_read_is_refused_not_sent_anonymously, and test_owner_pick_paid_reservation
::test_invariant_5_policy_off_blocks_auto_but_not_an_explicit_pick.

The opt-in is the owner's two facts as the gate reads them: a stored provider key slot and the
cloud consent (``free_cloud_enabled``). It is set at the gate's two readers
(``cloud_key_stored``, ``owner_turned_cloud_on``), not by writing a key into the row's credential
store or flipping its ``cloud_escalation.json``: several rows pin what happens with the free lane
off or with a mocked credential read, and a real key or a real policy change would change what they
test. No listed row's assertion changes. Off Windows the gate is open anyway, so the opt-in changes
nothing there.

``CLOUD-OPTIN-99.tsv`` is the count fence: exactly ``EXPECTED_COUNT`` unique node ids, and a listed
row whose file is collected but whose id is not is a collection error, so the list cannot drift.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

TSV = Path(__file__).with_name("CLOUD-OPTIN-99.tsv")
EXPECTED_COUNT = 99
FIXTURE = "windows_cloud_opted_in"


def optin_nodeids() -> tuple[str, ...]:
    rows = [line.split("\t", 1)[0].strip() for line in TSV.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    if len(rows) != EXPECTED_COUNT or len(set(rows)) != len(rows):
        raise pytest.UsageError(f"{TSV.name}: expected {EXPECTED_COUNT} unique rows, found {len(rows)} ({len(set(rows))} unique)")
    return tuple(rows)


@pytest.fixture()
def windows_cloud_opted_in(monkeypatch):
    """The gate sees a stored provider key slot and cloud turned on (free_cloud_enabled)."""
    from core import windows_cloud_gate

    monkeypatch.setattr(windows_cloud_gate, "cloud_key_stored", lambda: True)
    monkeypatch.setattr(windows_cloud_gate, "owner_turned_cloud_on", lambda: True)
    yield


def apply(items: list) -> None:
    """Add the opt-in to the listed rows only, and fence the list against the collected rows."""
    wanted = set(optin_nodeids())
    collected_files = {item.nodeid.split("::", 1)[0] for item in items}
    seen: set[str] = set()
    for item in items:
        if item.nodeid in wanted:
            seen.add(item.nodeid)
            if FIXTURE not in item.fixturenames:
                item.fixturenames.append(FIXTURE)
    stale = sorted(nodeid for nodeid in wanted - seen if nodeid.split("::", 1)[0] in collected_files)
    if stale:
        raise pytest.UsageError(f"{TSV.name} names rows that were not collected: {stale}")


# ---- re-pins: rows whose premise is "no key" / "no consent" are not opted in --------------------
#
# A row whose premise is no stored key, no consent, a fresh install or no opt-in is not given the
# opt-in: on Windows (core.windows_cloud_gate.active(), the car's seam) it checks the new rule
# instead -- the cloud door stays closed, no socket opens, and the answer is OFF_TEXT. Off Windows it
# checks what it always did. These helpers state the Windows half once.


def cloud_gate_active() -> bool:
    """Whether this process enforces the Windows cloud rule (the car's own seam)."""
    from core import windows_cloud_gate

    return windows_cloud_gate.active()


def off_text() -> str:
    from core import windows_cloud_gate

    return windows_cloud_gate.OFF_TEXT


def record_sockets(monkeypatch) -> list[tuple[str, str]]:
    """Patch socket name lookup and connect to record and refuse; returns the record."""
    import socket

    seen: list[tuple[str, str]] = []

    def _getaddrinfo(host, *_args, **_kwargs):
        seen.append(("getaddrinfo", str(host or "")))
        raise OSError("test: no socket before opt-in")

    def _connect(self, address):
        seen.append(("connect", str(address[0] if isinstance(address, tuple) else address)))
        raise OSError("test: no socket before opt-in")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    return seen


def assert_cloud_transport_closed(monkeypatch, host: str, url: str) -> None:
    """Windows, no stored key: a PolicyBoundCloudTransport to ``host`` is closed, ``request_json``
    to ``url`` refuses with OFF_TEXT, and no socket (not even a name lookup) opens."""
    from core.cloud_transport import CloudTransportError, PolicyBoundCloudTransport

    seen = record_sockets(monkeypatch)
    sent: list[str] = []
    transport = PolicyBoundCloudTransport(
        allowed_hosts=(host,), requester=lambda *a, **k: sent.append(str(a or k)))
    assert transport._network_allowed() is False
    with pytest.raises(CloudTransportError) as refused:
        transport.request_json(method="POST", url=url, body={})
    assert off_text() in str(refused.value), str(refused.value)
    assert sent == [] and seen == [], (sent, seen)


@contextlib.contextmanager
def fresh_profile(path: Path) -> Iterator[None]:
    """A fresh profile home for the block: no stored key and no cloud consent, whatever an earlier
    row left in the session home."""
    from core import runtime_paths

    previous_home = runtime_paths._VOOL_HOME_OVERRIDE
    runtime_paths.configure_runtime_home(path)
    try:
        yield
    finally:
        runtime_paths.configure_runtime_home(previous_home)


def assert_lane_says_cloud_is_off(lane, run, wire, monkeypatch) -> None:
    """Windows, no stored key: the cloud lane's health check and its call answer OFF_TEXT, the wire
    is never called and no socket opens. ``run(lane)`` makes the row's own call."""
    seen = record_sockets(monkeypatch)
    health = lane.health_check()
    assert health["ok"] is False and health.get("error") == off_text(), health
    with pytest.raises(RuntimeError) as refused:
        run(lane)
    assert str(refused.value) == off_text(), str(refused.value)
    wire.assert_not_called()
    assert seen == [], seen
