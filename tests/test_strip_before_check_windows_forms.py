"""Path gates judge the raw text before stripping, so Windows forms are not laundered.

`write_demand._clean_relative_path` and `audit_policy.audit_tool_permission_denial` used to strip
leading characters before judging `..` and the drive. Plain strings are used so these cases fail
on the unfixed code on every host. Every write_demand case names a file with an extension, so the
unfixed code accepts it and only the raw-text check can refuse it.
"""

from __future__ import annotations

import pytest

from core.agent_runtime.audit_policy import audit_tool_permission_denial
from core.execution.write_demand import _clean_relative_path

_CRAFTED_WRITE_DEMAND = [
    r"./C:\x.txt",
    r"/C:\Users\you\evil.py",
    r"..\evil.txt",
    r"../..\evil.txt",
    r"./..\x.txt",
    r"\generated\x.txt",
    r"C:generated\x.txt",
    r"\\server\share\x.txt",
]

_CRAFTED_AUDIT = [
    r"./C:\x.json",
    r"/C:\x.json",
    r"..\evil.json",
    r"../..\evil.json",
    r"/generated/x",
    r"\generated\x",
    r"C:\generated\x",
    r"C:generated\x",
    r"\\server\share\generated\x",
]

_CONTEXT = {"audit_execution_policy": {"proof_write_scope": "repository_generated"}}


def _audit_denial(path: str) -> str:
    return audit_tool_permission_denial("fs.write", "workspace_write", _CONTEXT, {"path": path})


@pytest.mark.parametrize("path", _CRAFTED_WRITE_DEMAND)
def test_write_demand_refuses_crafted_windows_forms(path: str) -> None:
    assert _clean_relative_path(path) == ""


@pytest.mark.parametrize("path", _CRAFTED_AUDIT)
def test_audit_policy_refuses_crafted_windows_forms(path: str) -> None:
    assert _audit_denial(path) != ""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("generated/x.json", "generated/x.json"),
        ("docs/notes.txt", "docs/notes.txt"),
    ],
)
def test_write_demand_still_accepts_ordinary_relative_paths(path: str, expected: str) -> None:
    assert _clean_relative_path(path) == expected


@pytest.mark.parametrize("path", ["generated/x.json", "./generated/x.json", "generated\\x.json"])
def test_audit_policy_still_accepts_generated_paths(path: str) -> None:
    assert _audit_denial(path) == ""
