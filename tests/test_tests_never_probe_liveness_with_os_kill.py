"""Tests ask whether a process exists through core.process_liveness.pid_alive, never os.kill(pid, 0).

On Windows CPython maps os.kill(pid, 0) to TerminateProcess: the "probe" kills a live process, and
for a gone one it raises a plain OSError, not ProcessLookupError. A test that checks "the child
was torn down" that way passes on Windows because it did the killing itself, and one that expects
ProcessLookupError fails there although the process is gone. pid_alive asks the kernel instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _os_kill_zero_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "kill"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "os"):
            continue
        signal_arg = node.args[1]
        if isinstance(signal_arg, ast.Constant) and signal_arg.value == 0:
            lines.append(node.lineno)
    return lines


def test_no_test_probes_a_process_with_os_kill_zero() -> None:
    offenders = [
        f"{path.relative_to(TESTS.parent).as_posix()}:{line}"
        for path in sorted(TESTS.rglob("*.py"))
        for line in _os_kill_zero_calls(path)
    ]
    assert offenders == [], "use core.process_liveness.pid_alive(pid) instead: " + ", ".join(offenders)
