from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class ProviderHealth:
    provider_id: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    timeout_failures: int = 0
    circuit_open_until: float = 0.0
    last_error: str | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    half_open_trial_started_at: float | None = None

    @property
    def circuit_open(self) -> bool:
        return self.circuit_open_until > time.time()


_LOG = logging.getLogger(__name__)

_HEALTH: dict[str, ProviderHealth] = {}
# A9: one concurrent failing turn per provider was enough to race the read-modify-write
# counters (`consecutive_failures += 1` spans several bytecodes), losing increments and
# delaying or never opening the circuit. Every registry mutation and lookup is serialized;
# health is a machine fact shared by all chats, so a single registry lock is the right scope.
_HEALTH_LOCK = threading.RLock()


def reset_provider_health(provider_id: str | None = None) -> None:
    with _HEALTH_LOCK:
        if provider_id is None:
            _HEALTH.clear()
            return
        _HEALTH.pop(provider_id, None)


def get_provider_health(provider_id: str) -> ProviderHealth:
    with _HEALTH_LOCK:
        if provider_id not in _HEALTH:
            _HEALTH[provider_id] = ProviderHealth(provider_id=provider_id)
        return _HEALTH[provider_id]


def circuit_is_open(provider_id: str) -> bool:
    return get_provider_health(provider_id).circuit_open


# Half-open recovery. An open circuit used to refuse EVERY call until its cooldown ran out, so one
# slow turn on the only local model (a long malformed tool call, then a timeout, with the GPU full)
# made the user's next plain questions fail in milliseconds without the model ever being asked.
# While the circuit is open, one trial call may now go through: never within a few seconds of the
# last failure (so a turn's own immediate retries still fail fast), and never while another trial
# is in flight (so a struggling provider is not hammered). The trial's own success closes the
# circuit; its failure re-opens it as any failure does. A trial that ends with neither (a refusal
# before sending) frees its lease after HALF_OPEN_TRIAL_LEASE_SECONDS.
HALF_OPEN_MIN_SPACING_SECONDS: float = 5.0
HALF_OPEN_TRIAL_LEASE_SECONDS: float = 180.0


def admit_provider_call(provider_id: str) -> bool:
    """True when a call to ``provider_id`` may be sent now: the circuit is closed, or it is open and
    this caller has just taken the single half-open trial."""
    with _HEALTH_LOCK:
        state = get_provider_health(provider_id)
        now = time.time()
        if state.circuit_open_until <= now:
            return True
        started = state.half_open_trial_started_at
        if started is not None and now - started < HALF_OPEN_TRIAL_LEASE_SECONDS:
            return False
        last_failure = state.last_failure_at
        if last_failure is not None and now - last_failure < HALF_OPEN_MIN_SPACING_SECONDS:
            return False
        state.half_open_trial_started_at = now
    _LOG.info("provider %s: circuit open, sending one half-open trial call", provider_id)
    return True


# Default time-to-live for trusting a recent successful invocation as an
# implicit health signal, in seconds. After a success the per-invocation probe
# can be skipped until this window elapses.
DEFAULT_HEALTH_PROBE_TTL_SECONDS: float = 30.0


def should_probe_health(provider_id: str, *, ttl_seconds: float = DEFAULT_HEALTH_PROBE_TTL_SECONDS) -> bool:
    """Return True when a per-invocation health probe is warranted.

    The probe is skipped only when the provider's circuit is closed AND it
    succeeded within ``ttl_seconds`` ago. The probe is always run on first use
    (no recorded success), when the circuit is open, once the TTL elapses, and
    after any failure (a failure clears the recent-success window because a
    later failure updates ``last_failure_at`` without refreshing
    ``last_success_at``).
    """
    state = get_provider_health(provider_id)
    if state.circuit_open:
        return True
    last_success = state.last_success_at
    if last_success is None:
        return True
    if ttl_seconds <= 0:
        return True
    last_failure = state.last_failure_at
    if last_failure is not None and last_failure >= last_success:
        # The most recent signal was a failure; re-probe before trusting it.
        return True
    return (time.time() - last_success) >= ttl_seconds


def record_provider_success(provider_id: str) -> None:
    with _HEALTH_LOCK:
        state = get_provider_health(provider_id)
        was_open = state.circuit_open_until > 0.0
        state.total_successes += 1
        state.consecutive_failures = 0
        state.last_success_at = time.time()
        state.last_error = None
        state.circuit_open_until = 0.0
        state.half_open_trial_started_at = None
    if was_open:
        _LOG.info("provider %s: circuit closed after a successful call", provider_id)


def record_provider_failure(
    provider_id: str,
    *,
    error: str,
    timeout: bool = False,
    failure_threshold: int = 5,
    cooldown_seconds: int = 20,
) -> ProviderHealth:
    with _HEALTH_LOCK:
        state = get_provider_health(provider_id)
        state.total_failures += 1
        state.consecutive_failures += 1
        state.last_failure_at = time.time()
        state.last_error = error
        state.half_open_trial_started_at = None
        if timeout:
            state.timeout_failures += 1
        opened = state.consecutive_failures >= max(1, failure_threshold)
        if opened:
            state.circuit_open_until = time.time() + max(1, cooldown_seconds)
    if opened:
        _LOG.warning(
            "provider %s: circuit open for %ss after %s consecutive failures (last: %s)",
            provider_id, max(1, cooldown_seconds), state.consecutive_failures, str(error)[:200],
        )
    return state


def provider_health_snapshot(provider_id: str) -> dict[str, object]:
    return asdict(get_provider_health(provider_id)) | {"circuit_open": circuit_is_open(provider_id)}
