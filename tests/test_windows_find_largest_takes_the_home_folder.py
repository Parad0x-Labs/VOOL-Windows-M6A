"""On Windows, `machine.find_largest` named with the whole home folder scans it, as elsewhere.

With no `drive` the tool scans the whole system drive on Windows, which holds the home folder, so naming
the home folder is no wider than the default. Before this, the Windows branch accepted only a bare drive
letter and sent the home folder to the Desktop/Downloads/Documents check, which refused it. Folders
inside the home folder outside those three stay refused.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from core import runtime_execution_tools
from core.runtime_execution_tools import execute_runtime_tool


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve() / "home"
    (root / "Desktop" / "project").mkdir(parents=True)
    (root / "Desktop" / "project" / "small.bin").write_bytes(b"x" * 1024)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "known_hosts").write_text("real ssh data\n", encoding="utf-8")
    monkeypatch.setattr(runtime_execution_tools, "_machine_home", lambda: root)
    monkeypatch.setattr(runtime_execution_tools, "_is_windows_platform", lambda: True)
    return root


def _find_largest(home: Path, drive: str):
    with mock.patch.object(runtime_execution_tools.windows_known_folders, "safe_folder",
                           lambda name, base: base / name):
        return execute_runtime_tool("machine.find_largest", {"drive": drive, "kind": "files"},
                                    source_context={"workspace": str(home / "Desktop" / "project")})


def test_the_whole_home_folder_can_be_named_on_windows(home: Path) -> None:
    result = _find_largest(home, str(home))
    assert result.ok is True and result.status == "executed", (result.status, result.response_text)


def test_a_folder_inside_home_outside_the_safe_folders_stays_refused_on_windows(home: Path) -> None:
    result = _find_largest(home, str(home / ".ssh"))
    assert result.ok is False and result.status == "not_allowed", (result.status, result.response_text)
    assert "known_hosts" not in result.response_text
