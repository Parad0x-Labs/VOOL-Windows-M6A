"""Two subtasks that only touch in time did not run at the same time.

`concurrency_report` sweeps half-open [started_at, completed_at) intervals, and
`SubtaskOutcome.overlaps` is strict, so a subtask that starts at the very instant the previous one
completed is sequential. On Windows `time.monotonic()` ticks about every 15.6 ms, so a sequential
run often records exactly that, and the report must still say max_concurrent == 1 (the Windows
sabotage run reported 2 with no overlapping pair). The timings here are set by hand, so this
runs the same on every OS.
"""

from __future__ import annotations

from core.agent_runtime.live_data_runner import concurrency_report
from core.live_data_plan import LiveDataSubtask, SubtaskOutcome


def _outcome(name: str, started: float, completed: float) -> SubtaskOutcome:
    subtask = LiveDataSubtask(subtask_id=name, entity=name, operation="market_quote", arguments={},
                              required_result_fields=(), tool="market_prices", tool_intent="web.research")
    return SubtaskOutcome(subtask=subtask, started_at=started, completed_at=completed)


def test_back_to_back_subtasks_with_one_shared_instant_are_sequential() -> None:
    report = concurrency_report([_outcome("a", 0.0, 1.0), _outcome("b", 1.0, 2.0), _outcome("c", 2.0, 3.0)])
    assert report.max_concurrent == 1
    assert report.overlapping_pairs == ()
    assert report.proves_concurrency is False


def test_a_real_overlap_still_counts() -> None:
    report = concurrency_report([_outcome("a", 0.0, 1.0), _outcome("b", 0.5, 2.0), _outcome("c", 2.0, 3.0)])
    assert report.max_concurrent == 2
    assert report.overlapping_pairs == (("a", "b"),)
    assert report.proves_concurrency is True


def test_simultaneous_starts_count_every_runner() -> None:
    report = concurrency_report([_outcome(name, 0.0, 1.0) for name in ("a", "b", "c")])
    assert report.max_concurrent == 3
    assert len(report.overlapping_pairs) == 3
