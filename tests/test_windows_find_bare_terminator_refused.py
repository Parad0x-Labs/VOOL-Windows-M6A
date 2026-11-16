"""Windows: a find whose -exec/-ok terminator is left bare is still refused, and nothing starts.

The program-check row (test_windows_command_line_parse.py) quotes the terminator so it reaches the -exec/-ok
check. These bare forms are refused earlier, by Mac 0.7.3's command-line card; whatever refuses them, they
must stay refused, so this row fails if that ever stops.
"""
from __future__ import annotations

import pytest

from tests.test_windows_command_line_parse import _program, _run_in_sandbox, windows


@pytest.mark.parametrize(
    "command",
    ["find . -exec git status ;", r"find . -exec git status \;", "find . -ok x.exe {} ;"],
)
def test_a_find_with_a_bare_terminator_is_refused_and_starts_nothing(windows, monkeypatch, tmp_path, command):
    from core.execution_gate import ExecutionGate
    from sandbox.job_runner import JobRunner

    bin_dir = tmp_path / "bin"
    _program(bin_dir, "find.EXE")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PATHEXT", ".EXE")
    spawned: list[list[str]] = []
    monkeypatch.setattr(JobRunner, "run", lambda self, argv, *a, **k: spawned.append(list(argv)) or {})
    monkeypatch.setattr(ExecutionGate, "evaluate_command", lambda self, cmd, **k: {"decision": "allowed", "reason": ""})
    _runner, result = _run_in_sandbox(tmp_path, command)
    assert result["status"] == "blocked_by_policy", result
    assert spawned == []
