"""tests/_windows_run_refusal.py ends a test at the Windows refusal only for a run step.

A refused write, diff or rollback is not the by-design Windows run refusal: it must reach the
test's own assertions and fail there, never end the test green. Driven on every platform by
setting the helper's WINDOWS switch inside a marked test.
"""
from __future__ import annotations

import pytest

from tests import _windows_run_refusal as refusal

REFUSED = {
    "status": "sandbox_unavailable",
    "details": {"executed": False, "reason": refusal.WINDOWS_REASON},
}


@pytest.fixture
def windows_marked_test(monkeypatch):
    monkeypatch.setattr(refusal, "WINDOWS", True)
    token = refusal._STOPS_HERE.set(True)
    yield
    refusal._STOPS_HERE.reset(token)


@pytest.mark.parametrize(("intent", "arguments"), [
    ("workspace.run_tests", {}),
    ("workspace.run_lint", {}),
    ("sandbox.run_command", {"command": "python -V"}),
    ("repo.step", {"step_id": "red", "intent": "workspace.run_tests", "arguments": {}}),
    ("code.task.step", {"step_id": "repro", "intent": "workspace.run_tests", "arguments": {}}),
])
def test_a_refused_run_step_ends_the_test(windows_marked_test, intent, arguments) -> None:
    with pytest.raises(refusal.WindowsRefusedTheRun):
        refusal.stop_if_windows_refused(REFUSED, intent, arguments)


@pytest.mark.parametrize(("intent", "arguments"), [
    ("workspace.write_file", {"path": "a.py", "content": "x = 1\n"}),
    ("workspace.git_diff", {}),
    ("code.task.rollback", {}),
    ("repo.step", {"step_id": "fix", "intent": "workspace.write_file", "arguments": {}}),
    ("code.task.step", {"step_id": "diff", "intent": "workspace.git_diff", "arguments": {}}),
])
def test_a_refused_step_that_is_not_a_run_comes_back_to_the_test(windows_marked_test, intent, arguments) -> None:
    assert refusal.stop_if_windows_refused(REFUSED, intent, arguments) is REFUSED
