"""Windows refuses to run commands, and the tests that drive a run expect that refusal there.

Windows has no kernel confinement backend, so ``sandbox/job_runner.py`` raises
``KernelIsolationUnavailableError`` for every executable tool and there is deliberately no
override: a test run, lint run or command ends ``sandbox_unavailable`` with nothing executed.
The owner chose to keep that refusal (option A, 28 Oct 2026; the test list is in
jobs/windows-run-tools-decision/OPTION-A-PLAN.md).

A test that drives a run passes each run step's result through ``stop_if_windows_refused``. On
Mac and Linux it returns the result untouched, so every later line runs exactly as before. On
Windows, inside a test marked ``@stops_at_windows_run_refusal``, a refused step is checked for
what the refusal must look like (the Windows reason, nothing executed, no workspace change
recorded) and then ``WindowsRefusedTheRun`` is raised; the decorator catches that one exception
and ends the test there, so the test passes only after the refusal was checked. A test without
the decorator gets its result back untouched on Windows too, so a test that expects a refusal
itself is never cut short.
"""
from __future__ import annotations

import contextvars
import functools
import json
import os

WINDOWS = os.name == "nt"

WINDOWS_REASON = "Windows has no kernel confinement backend"

_STOPS_HERE = contextvars.ContextVar("stops_at_windows_run_refusal", default=False)


class WindowsRefusedTheRun(BaseException):
    """Raised only on Windows, after the refusal was checked.

    A ``BaseException`` so that no ``except Exception`` in a test or rig on the way up can
    swallow it and let the test carry on past a run that never happened."""


def _fields(result) -> tuple[str, dict, str]:
    if isinstance(result, dict):
        return str(result.get("status") or ""), dict(result.get("details") or {}), json.dumps(result, default=str)
    details = dict(getattr(result, "details", None) or {})
    text = str(getattr(result, "response_text", "") or "") + json.dumps(details, default=str)
    return str(getattr(result, "status", "") or ""), details, text


def stop_if_windows_refused(result):
    """Return ``result`` unchanged, unless this is Windows, the test is marked, and the step was refused."""
    if not WINDOWS or not _STOPS_HERE.get():
        return result
    status, details, text = _fields(result)
    tool_result = details.get("tool_result") if isinstance(details.get("tool_result"), dict) else {}
    if "sandbox_unavailable" not in (status, details.get("status"), tool_result.get("status")):
        return result
    assert WINDOWS_REASON in text, f"refused, but not for the Windows reason: {text[:2000]}"
    for where in (details, tool_result):
        if "executed" in where:
            assert where["executed"] is False, f"a refused run says it executed: {where}"
        coverage = (where.get("blackbox") or {}).get("coverage") if isinstance(where.get("blackbox"), dict) else None
        if coverage:
            assert coverage.get("drift_count") == 0, f"a refused run changed the workspace: {coverage}"
    raise WindowsRefusedTheRun(status)


RUN_INTENTS = frozenset({"workspace.run_tests", "workspace.run_lint", "sandbox.run_command"})


def stop_if_journal_shows_windows_refusal(journal: dict) -> None:
    """The same check for a served rig, whose run happens inside the server: read the code
    task journal's first run step, the one every later stage of the task depends on."""
    for step_id in journal.get("step_order") or []:
        step = journal["steps"][step_id]
        if step.get("intent") in RUN_INTENTS:
            result = step.get("result") or {}
            stop_if_windows_refused({"status": result.get("status", ""),
                                     "details": {"executed": step.get("executed"), "tool_result": result}})
            return


def stops_at_windows_run_refusal(test):
    """End the test where Windows refused its run step; anywhere else this changes nothing."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        token = _STOPS_HERE.set(True)
        try:
            return test(*args, **kwargs)
        except WindowsRefusedTheRun:
            return None
        finally:
            _STOPS_HERE.reset(token)

    return run
