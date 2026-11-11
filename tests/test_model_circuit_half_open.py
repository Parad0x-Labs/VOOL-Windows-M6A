"""An open provider circuit lets one half-open trial through (Steve round 16, qwen3:8b).

Five consecutive failures opened the circuit for 20 s and every call in the window failed in
milliseconds with reason=circuit_open, so the user's next plain question never reached the model.
"""
from __future__ import annotations

import logging
from unittest import mock

import pytest

from core import model_health
from core.model_health import (
    admit_provider_call,
    circuit_is_open,
    record_provider_failure,
    record_provider_success,
    reset_provider_health,
)

PID = "ollama-local:qwen3:8b"


@pytest.fixture
def clock():
    reset_provider_health()
    now = {"t": 10_000.0}
    with mock.patch("core.model_health.time.time", side_effect=lambda: now["t"]):
        yield now
    reset_provider_health()


def _open(clock) -> None:
    for _ in range(5):
        record_provider_failure(PID, error="provider transfer exceeded its wall-clock deadline", timeout=True)
    assert circuit_is_open(PID)


def test_a_closed_circuit_admits_every_call(clock) -> None:
    assert all(admit_provider_call(PID, trial_eligible=True) for _ in range(3))


def test_retries_right_after_the_failure_still_fail_fast(clock) -> None:
    _open(clock)
    clock["t"] += 1
    assert admit_provider_call(PID, trial_eligible=True) is False


def test_a_fresh_turn_gets_one_trial_and_only_one(clock) -> None:
    _open(clock)
    clock["t"] += model_health.HALF_OPEN_MIN_SPACING_SECONDS + 2
    assert admit_provider_call(PID, trial_eligible=True) is True
    # the trial is in flight: a concurrent call and the same turn's second attempt fail fast
    assert admit_provider_call(PID, trial_eligible=True) is False
    assert admit_provider_call(PID, trial_eligible=True) is False


def test_a_successful_trial_closes_the_circuit(clock) -> None:
    _open(clock)
    clock["t"] += 7
    assert admit_provider_call(PID, trial_eligible=True)
    record_provider_success(PID)
    assert not circuit_is_open(PID)
    assert all(admit_provider_call(PID, trial_eligible=True) for _ in range(3))


def test_a_failed_trial_reopens_and_the_next_turn_may_try_again(clock) -> None:
    _open(clock)
    clock["t"] += 7
    assert admit_provider_call(PID, trial_eligible=True)
    clock["t"] += 3
    record_provider_failure(PID, error="timeout", timeout=True)
    assert circuit_is_open(PID)
    assert admit_provider_call(PID, trial_eligible=True) is False  # within the spacing of that failure
    clock["t"] += model_health.HALF_OPEN_MIN_SPACING_SECONDS
    assert admit_provider_call(PID, trial_eligible=True) is True


def test_an_abandoned_trial_frees_its_lease(clock) -> None:
    for _ in range(5):
        record_provider_failure(PID, error="x", cooldown_seconds=3600)
    clock["t"] += 7
    assert admit_provider_call(PID, trial_eligible=True)
    clock["t"] += model_health.HALF_OPEN_TRIAL_LEASE_SECONDS - 1
    assert admit_provider_call(PID, trial_eligible=True) is False
    clock["t"] += 2
    assert admit_provider_call(PID, trial_eligible=True) is True


def test_the_cooldown_itself_is_unchanged(clock) -> None:
    state = record_provider_failure(PID, error="x", failure_threshold=1, cooldown_seconds=20)
    assert state.circuit_open_until == clock["t"] + 20
    clock["t"] += 21
    assert not circuit_is_open(PID)


def test_open_trial_and_close_are_logged(clock, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="core.model_health"):
        _open(clock)
        clock["t"] += 7
        admit_provider_call(PID, trial_eligible=True)
        record_provider_success(PID)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"provider {PID}: circuit open for 20s after 5 consecutive failures" in text
    assert f"provider {PID}: circuit open, sending one half-open trial call" in text
    assert f"provider {PID}: circuit closed after a successful call" in text


def test_an_ineligible_provider_keeps_the_strict_cooldown(clock) -> None:
    _open(clock)
    clock["t"] += 7
    assert admit_provider_call(PID) is False
    assert admit_provider_call(PID, trial_eligible=False) is False
    clock["t"] += 14  # past the 20 s cooldown
    assert admit_provider_call(PID) is True


class _Manifest:
    def __init__(self, provider_id: str, local: bool) -> None:
        self.provider_id = provider_id
        self.local = local


@pytest.mark.parametrize("windows,local,eligible", [
    (True, True, True),     # a local model on Windows: one trial
    (True, False, False),   # an open cloud circuit admits no trial (a trial would be a real call)
    (False, True, False),   # Mac and Linux keep the strict cooldown
    (False, False, False),
])
def test_only_a_local_model_on_windows_is_trialled(monkeypatch, windows, local, eligible) -> None:
    from core import memory_first_router

    monkeypatch.setattr("sandbox.job_runner._is_windows_platform", lambda: windows)
    monkeypatch.setattr("core.local_model_policy.manifest_is_local", lambda m: m.local)
    assert memory_first_router._half_open_trial_eligible(_Manifest(PID, local)) is eligible


def test_the_router_gate_asks_for_admission_only_when_the_circuit_is_open() -> None:
    import inspect

    from core.memory_first_router import MemoryFirstRouter

    source = " ".join(inspect.getsource(MemoryFirstRouter).split())
    assert ("if circuit_is_open(manifest.provider_id) and not admit_provider_call( "
            "manifest.provider_id, trial_eligible=_half_open_trial_eligible(manifest) ):") in source
