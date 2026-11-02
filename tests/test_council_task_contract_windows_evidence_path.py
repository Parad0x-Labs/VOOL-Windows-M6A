"""TASK_CONTRACT evidence_path: Windows-shaped escapes are refused like ``/x`` and ``../x``.

The inputs are plain strings, so these tests behave the same on Windows and Linux.
"""

from __future__ import annotations

import pytest

from core.council.cost_ladder import CostPolicy, SpendCeilings
from core.council.model_provenance import ModelIdentity
from core.council.task_contract import (
    MutationPermission,
    TaskContract,
    TaskContractError,
    TaskSeat,
)

_SEATS = (
    TaskSeat("seat-w", "builder", ModelIdentity("openai", "gpt-4o", harness="app-chat")),
    TaskSeat("seat-r1", "reviewer", ModelIdentity("anthropic", "claude-3-5-sonnet", harness="app-chat")),
    TaskSeat("seat-r2", "falsifier", ModelIdentity("google", "gemini-2.0-flash", harness="openrouter")),
)


def _contract(evidence_path: str) -> TaskContract:
    return TaskContract(
        task_id="T1",
        repo_root="/repo/checkouts/main",
        base_sha="a" * 40,
        objective="Repair the council seat leak",
        writable_scope=("core/council",),
        authority_owner="operator:sls_0x",
        done_condition="the containment tests pass",
        verification="pytest tests/test_council_seat_containment.py",
        counterexample="a fenced seat still writes via a symlinked parent",
        evidence_path=evidence_path,
        cost_policy=CostPolicy(
            ceilings=SpendCeilings(
                max_calls=4, max_tokens=100_000, max_cost_usd=0.50, wall_clock_seconds=900.0
            )
        ),
        mutation=MutationPermission.WORKSPACE_WRITE,
        seats=_SEATS,
    )


@pytest.mark.parametrize(
    "evidence_path",
    [
        "/x",
        "../x",
        "C:\\x",
        "C:/x",
        "C:x",
        "c:x\\y",
        "\\\\server\\share\\x",
        "//server/share/x",
        "\\x",
        "..\\x",
        "evidence\\..\\..\\x",
        "evidence/..\\x",
        "evidence\\T1.jsonl:stream",
    ],
)
def test_escaping_or_absolute_evidence_path_is_refused(evidence_path: str) -> None:
    with pytest.raises(TaskContractError, match="evidence_path"):
        _contract(evidence_path)


@pytest.mark.parametrize(
    "evidence_path", ["evidence/T1.jsonl", "evidence\\T1.jsonl", "evidence/a..b.jsonl"]
)
def test_relative_evidence_path_is_still_accepted(evidence_path: str) -> None:
    assert _contract(evidence_path).evidence_path == evidence_path
