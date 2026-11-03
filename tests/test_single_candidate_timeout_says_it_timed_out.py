"""A sole local model that ran out its read timeout is reported as a timeout, not as a bad reply.

Mac Intel smoke, "recall answers from memory" (runs 41531657501, 41568073330, 41597602096): the
recall answer from qwen3:8b on a CPU-only Mac ran past the local lane's 180 s timeout, and the turn
said the model "did not return a usable reply". That reads as a model fault and hid the timeout.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.agent_runtime.memory_runtime import _single_candidate_failure_hint
from core.context_history_authority import assistant_text_is_runtime_failure_notice

_READ_TIMEOUT = (
    "HTTPConnectionPool(host='127.0.0.1', port=11434): Read timed out. (read timeout=180.0)"
)


def _sole_candidate(error: str, *, timeout_seconds: float = 180.0) -> SimpleNamespace:
    provider = "ollama-local:qwen3:8b"
    return SimpleNamespace(
        source="all_ranked_providers_failed",
        details={
            "attempted": [provider],
            "ranked_candidates": [provider],
            "attempted_error_reasons": [error],
            "attempt_timings": [
                {
                    "provider_id": "ollama-local",
                    "model_id": "qwen3:8b",
                    "seconds": timeout_seconds + 0.4,
                    "outcome": "failed",
                    "timeout_seconds": timeout_seconds,
                    "error": error,
                }
            ],
        },
    )


def test_a_sole_model_that_timed_out_is_reported_as_a_timeout() -> None:
    message = _single_candidate_failure_hint(_sole_candidate(_READ_TIMEOUT))
    assert "timed out after 180 s" in message
    assert "did not return a usable reply" not in message
    # Still the single-model notice, so it closes its exchange in replayed history.
    assert "was the only model this turn was allowed to use" in message
    assert assistant_text_is_runtime_failure_notice(message)


def test_the_timeout_named_is_the_one_the_attempt_ran_under() -> None:
    message = _single_candidate_failure_hint(_sole_candidate(_READ_TIMEOUT, timeout_seconds=96.5))
    assert "timed out after 96 s" in message


def test_a_sole_model_that_failed_otherwise_keeps_the_usable_reply_wording() -> None:
    message = _single_candidate_failure_hint(_sole_candidate("Expecting value: line 1 column 1 (char 0)"))
    assert "did not return a usable reply" in message
    assert "timed out" not in message
