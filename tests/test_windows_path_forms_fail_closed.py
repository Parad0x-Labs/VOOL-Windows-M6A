"""Windows path spellings never pass a check written for POSIX paths (sweep S8).

Each check below once knew only `/`: a drive path (`C:\\x`, `C:x`, `C:/x`), a path rooted at the
current drive (`\\x`) or a `..` written with a backslash read as an ordinary workspace-relative
name. On Windows every one of them lands outside the workspace. The checks are lexical, so they
answer the same on every host and the tests need no Windows."""

from __future__ import annotations

import re

import pytest

from core.code_assistant.contract import REASON_UNCONFINED_PATH, ContractRefused, _refuse_unconfined
from core.control_plane.templates import paths_from_payload
from core.execution.planner import _clean_workspace_path

_OUTSIDE = ("C:\\Users\\you\\secrets.txt", "C:/Users/you/secrets.txt", "C:secrets.txt", "\\Windows\\win.ini", "..\\evil", "src\\..\\..\\evil")


@pytest.mark.parametrize("path", _OUTSIDE)
def test_the_code_assistant_contract_refuses_a_windows_path_outside_the_workspace(path: str) -> None:
    with pytest.raises(ContractRefused) as refused:
        _refuse_unconfined(path)
    assert refused.value.reason == REASON_UNCONFINED_PATH


@pytest.mark.parametrize("path", ("src/app.py", "src\\app.py", "docs/a..b.md"))
def test_the_code_assistant_contract_still_takes_a_workspace_relative_path(path: str) -> None:
    _refuse_unconfined(path)


@pytest.mark.parametrize("path", ("C:\\Users\\you\\evil", "C:/evil", "notes\\..\\..\\evil", "a/b\\..\\..\\..\\evil"))
def test_the_planner_never_cleans_a_windows_escape_into_a_workspace_path(path: str) -> None:
    assert _clean_workspace_path(path) == ""


@pytest.mark.parametrize(("typed", "clean"), (("notes\\today", "notes\\today"), ("/notes", "notes")))
def test_the_planner_still_places_a_workspace_folder(typed: str, clean: str) -> None:
    assert _clean_workspace_path(typed) == clean


@pytest.mark.parametrize("path", ("C:\\Users\\you\\report.md", "C:/Users/you/report.md", "\\\\host\\share\\report.md"))
def test_a_receipt_lists_a_windows_path_it_touched(path: str) -> None:
    assert path in paths_from_payload({"note": path}, path_key_pattern=re.compile(r"(?!)"))
