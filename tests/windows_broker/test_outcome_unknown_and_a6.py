"""What a lost broker reply becomes, and which broker-backed intents A6 reserves
(docs/WINDOWS_BROKER.md section 8, Phase C slice C5).

A change request written to the broker whose reply was lost, or a batch the broker could not put
back, is VOOL's own EffectOutcomeUnknown (core.effect_reconciliation), so the executor records an
A6-reserved intent holding a claim as `unknown`, not safe to retry, and never as a plain failure.
It stays the client's EffectOutcomeUnknown too, for callers that catch that.

On Windows, while this process routes writes through the broker, machine.ensure_directory,
machine.move_path and code.task.rollback are A6-reserved as well; Mac and Linux keep today's list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import effect_reconciliation, windows_write_gate
from core.windows_broker import protocol, session, writes
from core.windows_broker.protocol import BatchNotRestored

from .test_single_writes import _Broker, _wire

NEWLY_RESERVED = ("machine.ensure_directory", "machine.move_path", "code.task.rollback")


@pytest.fixture
def flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "flagged"
    folder.mkdir()
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    return folder.resolve()


def test_a_lost_reply_is_vools_own_outcome_unknown(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _Broker(flagged, fail={"write_file": protocol.EffectOutcomeUnknown("broker response lost")})
    _wire(monkeypatch, broker)
    with pytest.raises(effect_reconciliation.EffectOutcomeUnknown) as raised:
        writes.write_file(flagged / "a.txt", b"x", "absent")
    assert isinstance(raised.value, protocol.EffectOutcomeUnknown)
    assert raised.value.reason == "broker_reply_lost"
    assert "broker response lost" in raised.value.detail


def test_a_batch_not_put_back_is_outcome_unknown_and_keeps_what_is_left(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch = {"batch_id": "b9", "ran": 1, "restored": False, "left": ["a.txt"]}
    broker = _Broker(flagged, fail={"write_file": BatchNotRestored("restore_incomplete", "left", "io_error", batch)})
    _wire(monkeypatch, broker)
    with pytest.raises(effect_reconciliation.EffectOutcomeUnknown) as raised:
        writes.write_file(flagged / "a.txt", b"x", "absent")
    assert raised.value.reason == "broker_batch_not_restored"
    assert raised.value.batch == batch  # type: ignore[attr-defined]
    assert "a.txt" in raised.value.detail


def test_a_reply_that_makes_no_sense_is_outcome_unknown(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _Broker(flagged)
    original = broker.request
    broker.request = lambda operation, **arguments: (  # type: ignore[method-assign]
        {"ok": True, "results": []} if operation == "write_file" else original(operation, **arguments))
    _wire(monkeypatch, broker)
    with pytest.raises(effect_reconciliation.EffectOutcomeUnknown) as raised:
        writes.write_file(flagged / "a.txt", b"x", "absent")
    assert raised.value.reason == "broker_reply_unclear"


@pytest.mark.parametrize("intent", NEWLY_RESERVED)
def test_broker_backed_intents_are_reserved_on_windows_while_routed(flagged: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    intent: str) -> None:
    _wire(monkeypatch, _Broker(flagged))
    assert effect_reconciliation.a6_reserved_intent(intent)


@pytest.mark.parametrize("intent", NEWLY_RESERVED)
def test_the_list_is_unchanged_without_a_routed_broker(flagged: Path, monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    assert not effect_reconciliation.a6_reserved_intent(intent)  # Windows, no broker started
    _wire(monkeypatch, _Broker(flagged))
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    assert not effect_reconciliation.a6_reserved_intent(intent)  # Mac and Linux


def test_the_existing_list_still_holds_everywhere() -> None:
    for intent in ("workspace.write_file", "machine.write_file", "operator.move_path", "workspace.rollback_last_change"):
        assert effect_reconciliation.a6_reserved_intent(intent)
    assert not effect_reconciliation.a6_reserved_intent("respond.direct")
