"""Immutable validation evidence for model answer attempts.

The provider-call ledger answers how many calls entered the provider. This ledger answers a
different question: which returned answer candidates passed the response controls and which one,
if any, was allowed to author the final response.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelAttemptValidation:
    """One immutable response-control verdict for one model answer attempt."""

    attempt_number: int
    provider_id: str
    model_id: str
    model_call_id: str
    raw_output: str
    raw_output_validation: dict[str, Any]
    ordinary_chat_output: dict[str, Any]
    response_language: dict[str, Any]
    provider_completion: dict[str, Any]
    failure_reasons: tuple[str, ...]
    retry_requested: bool
    accepted: bool

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-safe projection so later attempts cannot mutate this record."""

        reasons = list(self.failure_reasons)
        return {
            "attempt_number": self.attempt_number,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_call_id": self.model_call_id,
            "raw_output": self.raw_output,
            "raw_output_validation": deepcopy(self.raw_output_validation),
            "ordinary_chat_output": deepcopy(self.ordinary_chat_output),
            "response_language": deepcopy(self.response_language),
            "provider_completion": deepcopy(self.provider_completion),
            "failure_reasons": reasons,
            "retry_requested": self.retry_requested,
            "accepted": self.accepted,
        }


class ModelAttemptValidationLedger:
    """Append-only response-control evidence for a bounded model retry sequence."""

    def __init__(self) -> None:
        self._attempts: list[ModelAttemptValidation] = []

    def record(
        self,
        *,
        attempt_number: int,
        provider_id: str,
        model_id: str,
        model_call_id: str,
        raw_output: str,
        raw_output_validation: dict[str, Any] | None,
        ordinary_chat_output: dict[str, Any] | None,
        response_language: dict[str, Any] | None,
        provider_completion: dict[str, Any] | None,
        failure_reasons: tuple[str, ...] | list[str] | None,
        retry_requested: bool,
        accepted: bool,
    ) -> ModelAttemptValidation:
        """Append one detached verdict and return the frozen record."""

        record = ModelAttemptValidation(
            attempt_number=int(attempt_number),
            provider_id=str(provider_id or ""),
            model_id=str(model_id or ""),
            model_call_id=str(model_call_id or ""),
            raw_output=str(raw_output or ""),
            raw_output_validation=deepcopy(dict(raw_output_validation or {})),
            ordinary_chat_output=deepcopy(dict(ordinary_chat_output or {})),
            response_language=deepcopy(dict(response_language or {})),
            provider_completion=deepcopy(dict(provider_completion or {})),
            failure_reasons=tuple(str(reason) for reason in (failure_reasons or ()) if str(reason)),
            retry_requested=bool(retry_requested),
            accepted=bool(accepted),
        )
        self._attempts.append(record)
        return record

    @property
    def attempts(self) -> tuple[ModelAttemptValidation, ...]:
        return tuple(self._attempts)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self._attempts]

    @property
    def accepted_attempt(self) -> ModelAttemptValidation | None:
        for record in reversed(self._attempts):
            if record.accepted:
                return record
        return None
