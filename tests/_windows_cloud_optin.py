"""One opt-in for the existing rows that drive cloud paths, so they keep their meaning on Windows.

0.7.1 for Windows sends nothing to a cloud model provider until the owner has stored a key AND
turned cloud on (core/windows_cloud_gate.py). Rows written before that rule drive the OpenRouter
catalog, the chat cloud intents or a BYOK lane (on a mocked wire) in a profile with neither, so on
Windows the gate answers "Cloud models are off..." before the behaviour the row pins is reached
(IM Windows run 42783582031: exactly the 60 rows in CLOUD-OPTIN-60.tsv).

The opt-in is the owner's two facts as the gate reads them: a stored provider key slot and the
cloud consent (``free_cloud_enabled``). It is set at the gate's two readers
(``cloud_key_stored``, ``owner_turned_cloud_on``), not by writing a key into the row's credential
store or flipping its ``cloud_escalation.json``: several rows pin what happens with NO usable saved
key, or with the free lane off, and a real key or a real policy change would change what they test.
No assertion changes. Off Windows the gate is open anyway, so the opt-in changes nothing there.

``CLOUD-OPTIN-60.tsv`` is the count fence: exactly ``EXPECTED_COUNT`` unique node ids, and a listed
row whose file is collected but whose id is not is a collection error, so the list cannot drift.
"""
from __future__ import annotations

from pathlib import Path

import pytest

TSV = Path(__file__).with_name("CLOUD-OPTIN-60.tsv")
EXPECTED_COUNT = 60
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
