"""Only a test's own tmp_path is listed as disposable on Windows, and only there (tests/_windows_disposable_tmp.py)."""
from __future__ import annotations

import os

from core import windows_write_gate as gate
from tests._windows_disposable_tmp import mark_own_tmp_path_disposable

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
        assert os.environ.get(gate.ENV_VAR) == str(tmp_path)
    else:
        assert os.environ.get(gate.ENV_VAR) == _INHERITED

