"""Only a test's own tmp_path is listed as disposable on Windows, and only there (tests/_windows_disposable_tmp.py)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core import windows_write_gate as gate
from tests._windows_disposable_tmp import mark_own_tmp_path_disposable, session_pytest_workspace

# what the shell running the suite set, if anything: off Windows the suite must leave it as it found it
_INHERITED = os.environ.get(gate.ENV_VAR)


def test_on_windows_only_the_tests_own_tmp_path_is_listed(tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    mark_own_tmp_path_disposable(monkeypatch, tmp_path, windows=True)
    assert os.environ[gate.ENV_VAR] == str(tmp_path)


def test_off_windows_nothing_is_listed(tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    mark_own_tmp_path_disposable(monkeypatch, tmp_path, windows=False)
    assert gate.ENV_VAR not in os.environ


def test_the_suite_lists_this_tests_folder_on_windows_and_nothing_elsewhere(tmp_path):
    if os.name == "nt":
        listed = os.environ.get(gate.ENV_VAR, "").split(os.pathsep)
        assert listed[0] == str(tmp_path) and len(listed) == 3
        assert tempfile.gettempdir() == listed[1]
        assert listed[2] == str(Path(os.environ["VOOL_HOME"]) / "workspace")
    else:
        assert os.environ.get(gate.ENV_VAR) == _INHERITED


# unittest rigs (test_a6_effect_reconciliation.py, test_a_read_only_tool_batch_runs_in_one_round.py
# and ~90 more) build their workspace with tempfile, not tmp_path. On Windows their tempfile folders
# land in a test-owned folder that is listed too, never in the system TEMP.
def test_on_windows_a_tempfile_workspace_lands_in_a_listed_test_folder(tmp_path_factory, tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    own_temp = tmp_path_factory.mktemp("tempfile-")
    mark_own_tmp_path_disposable(monkeypatch, tmp_path, windows=True, tempfile_root=own_temp)
    with tempfile.TemporaryDirectory(prefix="a6-") as workspace:
        assert Path(workspace).parent == own_temp
        listed = [Path(root) for root in os.environ[gate.ENV_VAR].split(os.pathsep)]
        assert listed == [tmp_path, own_temp]


def test_off_windows_tempfile_is_left_alone(tmp_path_factory, tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    before = tempfile.gettempdir()
    mark_own_tmp_path_disposable(monkeypatch, tmp_path, windows=False, tempfile_root=tmp_path_factory.mktemp("t-"))
    assert tempfile.gettempdir() == before
    assert gate.ENV_VAR not in os.environ


# Runtime code with no workspace of its own (an empty source_context) falls back to VOOL_HOME/workspace,
# and the gate counts that folder as a target. In the suite VOOL_HOME is the session home the root
# conftest made with mkdtemp (prefix vool_pytest_home_), so on Windows its workspace is listed too.
def test_on_windows_the_session_pytest_homes_workspace_is_listed(tmp_path_factory, tmp_path, monkeypatch):
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    home = tmp_path_factory.mktemp("vool_pytest_home_", numbered=True)
    monkeypatch.setenv("VOOL_HOME", str(home))
    mark_own_tmp_path_disposable(monkeypatch, tmp_path, windows=True, session_workspace=session_pytest_workspace())
    listed = [Path(root) for root in os.environ[gate.ENV_VAR].split(os.pathsep)]
    assert listed == [tmp_path, home / "workspace"]


# A VOOL_HOME the suite did not make (the real profile, or one a test points elsewhere) is never listed.
def test_a_home_the_suite_did_not_make_is_never_listed(tmp_path_factory, monkeypatch):
    for home in (Path.home() / ".vool", tmp_path_factory.mktemp("someone_elses_home")):
        monkeypatch.setenv("VOOL_HOME", str(home))
        assert session_pytest_workspace() is None
    monkeypatch.delenv("VOOL_HOME")
    assert session_pytest_workspace() is None
