"""Windows 0.7.1 stated limits, as test outcomes (tests/windows_0_7_1_limits.tsv).

VOOL 0.7.1 refuses git and plugins/MCP servers on Windows. The rows listed in the TSV test those
features and cannot pass on a Windows host. On Windows only, a listed row is reported as an expected
failure when, and only when, the exception that failed it IS a refusal: one of its limit's exception
types, raised from VOOL's own code, carrying the refusal sentence, and not raised by the in-process
git spawn guard (a test's own git, not VOOL's). An AssertionError never counts, whatever its text, so
a row that checks "refused and nothing changed" still fails when nothing-changed fails. A listed row
that passes fails, and the session fails when a listed row of a collected file was not collected.
Off Windows the list does nothing.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

LIST = Path(__file__).with_name("windows_0_7_1_limits.tsv")
NEVER_LISTED = ("tests/test_windows_confinement_attacks.py",)
#: Per limit: the exception types (module, qualified name) that are VOOL's refusal, and its sentence.
LIMITS = {
    "git": ({("core.safe_git", "GitRefusedOnWindows"), ("core.repoops.identity", "WorkspaceError"),
             ("core.project_archaeology", "ArchaeologyInputError"),
             ("core.project_archaeology", "GitRefused"), ("core.user_rules", "_ScanIncomplete")},
            "VOOL doesn't run git on Windows in 0.7.1"),
    "plugin": ({("core.plugin_lifecycle", "LifecycleError"), ("core.plugin_executor", "ConfinementUnavailableError"),
                ("core.mcp_client", "MCPRefusedOnWindows")},
               "VOOL doesn't start plugins or MCP servers on Windows in 0.7.1"),
}
GUARD_FRAME = "_spawn_audit"
_CORE = os.sep + "core" + os.sep


def load(path: Path = LIST) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        node, limit = line.split("\t")
        if limit not in LIMITS:
            raise ValueError(f"{path.name}: unknown limit {limit!r} for {node}")
        if node.startswith(NEVER_LISTED):
            raise ValueError(f"{path.name}: {node} must never be listed")
        rows[node] = limit
    return rows


ACTIVE = os.name == "nt"
ROWS: dict[str, str] = load() if ACTIVE else {}
EXCUSED: set[str] = set()


def is_refusal(limit: str, excinfo: pytest.ExceptionInfo | None) -> bool:
    """Whether the exception that failed a row is its limit's refusal, raised from VOOL's code."""
    if excinfo is None:
        return False
    types, sentence = LIMITS[limit]
    if (excinfo.type.__module__, excinfo.type.__qualname__) not in types:
        return False
    if sentence not in str(excinfo.value):
        return False
    frames = list(excinfo.traceback)
    if not frames or any(entry.name == GUARD_FRAME for entry in frames):
        return False
    return _CORE in str(frames[-1].path)


def node_of(item: pytest.Item) -> str:
    """The node id with its file part in forward slashes (a parameter id keeps its own backslashes)."""
    file, sep, rest = item.nodeid.partition("::")
    return file.replace("\\", "/") + sep + rest


def apply(item: pytest.Item, call: pytest.CallInfo, report: pytest.TestReport) -> None:
    node = node_of(item)
    limit = ROWS.get(node)
    if limit is None or report.when not in ("setup", "call") or report.skipped:
        return
    if report.passed:
        if report.when == "call":
            report.outcome = "failed"
            report.longrepr = (f"listed in {LIST.name} as a Windows 0.7.1 {limit} limit but passed: "
                               "remove the row (the limit no longer decides it)")
        return
    if is_refusal(limit, call.excinfo):
        EXCUSED.add(node)
        report.outcome = "skipped"
        report.wasxfail = f"Windows 0.7.1 limit: {limit} refused by design ({LIST.name})"


def missing(collected: set[str], args: list[str], rootdir: Path) -> list[str]:
    """Listed rows in the files or folders named whole on the command line that were not collected (a
    renamed or removed row). Files named by node id (``file::test``) are not checked."""
    whole = []
    for arg in args:
        if "::" in arg:
            continue
        path = Path(arg)
        path = path if path.is_absolute() else rootdir / path
        try:
            whole.append(path.resolve().relative_to(rootdir.resolve()).as_posix())
        except (OSError, ValueError):
            continue
    def named(node: str) -> bool:
        file = node.split("::", 1)[0]
        return any(name in (".", "") or file == name or file.startswith(name.rstrip("/") + "/") for name in whole)
    return sorted(node for node in ROWS if named(node) and node not in collected)
