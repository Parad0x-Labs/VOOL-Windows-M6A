"""Proposal-time confinement pre-check must refuse Windows-absolute paths on every host.

The inputs are plain strings, so the result does not depend on the host OS.
"""

from __future__ import annotations

import pytest

from core.code_assistant.journal_schema import PATH_KEYS
from core.code_assistant.task_runtime import CodeTaskRuntime

_UNCONFINED = [
    "C:\\Users\\x",
    "C:/Users/x",
    "c:\\x",
    "C:x",
    "c:x.txt",
    "Z:\\",
    "\\\\server\\share\\x",
    "//server/share/x",
    "\\x",
    "\\Windows\\x",
    "..\\x",
    "sub\\..\\..\\x",
    "/etc/passwd",
    "~/x",
]


@pytest.mark.parametrize("value", _UNCONFINED)
@pytest.mark.parametrize("key", PATH_KEYS)
def test_unconfined_path_is_refused(key: str, value: str) -> None:
    assert CodeTaskRuntime._refuse_unconfined_paths({key: value}) == value


@pytest.mark.parametrize("value", ["src/a.py", "src\\a.py", "a.py", "dir/sub/b.txt", "..foo/x", "a..b"])
def test_confined_relative_path_is_accepted(value: str) -> None:
    assert CodeTaskRuntime._refuse_unconfined_paths({"path": value}) == ""


def test_offending_path_among_several_keys_is_reported() -> None:
    args = {"source": "ok/a.py", "destination": "D:\\out\\b.py"}
    assert CodeTaskRuntime._refuse_unconfined_paths(args) == "D:\\out\\b.py"
