"""Windows 0.7.1 stated limits, as test outcomes (tests/windows_0_7_1_limits.tsv).

VOOL 0.7.1 refuses git, plugins/MCP servers and programs other than Python and pytest on Windows. The
rows listed in the TSV test those features and cannot pass on a Windows host. On Windows only, a listed
row is reported as an expected failure when, and only when, it fails with the refusal sentence of its
limit; a failure raised by the in-process git spawn guard (a test's own git, not VOOL's) or any failure
without that sentence stays red, and a listed row that passes fails so a lifted limit is noticed. Off
Windows the list does nothing.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

LIST = Path(__file__).with_name("windows_0_7_1_limits.tsv")
SENTENCES = {
    "git": ("VOOL doesn't run git on Windows in 0.7.1", "git_refused_on_windows", "VOOL doesn't... was changed."),
    "plugin": ("VOOL doesn't start plugins or MCP servers on Windows in 0.7.1",),
    "program": ("VOOL 0.7.1 runs only Python and pytest",),
}
GUARD_FRAME = "_spawn_audit"


def load(path: Path = LIST) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        node, limit = line.split("\t")
        if limit not in SENTENCES:
            raise ValueError(f"{path.name}: unknown limit {limit!r} for {node}")
        rows[node] = limit
    return rows


ACTIVE = os.name == "nt"
ROWS: dict[str, str] = load() if ACTIVE else {}


def outcome(limit: str, failed: bool, text: str) -> tuple[str, str] | None:
    """(outcome, reason) to report for a listed row, or None to leave the report as it is."""
    if not failed:
        return "failed", (f"listed in {LIST.name} as a Windows 0.7.1 {limit} limit but passed: "
                          "remove the row (the limit no longer decides it)")
    if GUARD_FRAME in text:
        return None
    if any(sentence in text for sentence in SENTENCES[limit]):
        return "skipped", f"Windows 0.7.1 limit: {limit} refused by design ({LIST.name})"
    return None


def apply(item: pytest.Item, call: pytest.CallInfo, report: pytest.TestReport) -> None:
    limit = ROWS.get(item.nodeid.replace("\\", "/"))
    if limit is None or report.when not in ("setup", "call"):
        return
    if report.when == "setup" and report.passed:
        return
    if report.skipped:
        return
    text = str(report.longrepr or "")
    if call.excinfo is not None:
        text += "\n" + str(call.excinfo.value)
    decided = outcome(limit, report.failed, text)
    if decided is None:
        return
    kind, reason = decided
    if kind == "skipped":
        report.outcome = "skipped"
        report.wasxfail = reason
    else:
        report.outcome = "failed"
        report.longrepr = reason
