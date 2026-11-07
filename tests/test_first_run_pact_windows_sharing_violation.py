"""A pact file Windows cannot open for a moment is busy, never corrupt.

On Windows, opening the pact file while another writer's os.replace swaps it in, or
replacing it while a reader holds it open, can fail with PermissionError for a moment.
The pact retries through core.windows_sharing_retry. A failure that outlasts it answers
the typed lock_unavailable fault (409), on a GET too. It never quarantines the file or
reseeds the tour, because a reset tour would look like consent state changing. These
tests drive the Windows branch on any host through windows_sharing_retry._IS_WINDOWS.
"""
from __future__ import annotations

import builtins
import errno
import json
import os
from pathlib import Path

import pytest

from core import first_run_pact, windows_sharing_retry
from core.first_run_pact import PactFault
from tests.first_run_pact_rig import pact_rig


def _pact_file() -> Path:
    from core.runtime_paths import active_data_dir

    return active_data_dir() / "first_run_pact_state.json"


def _quarantined() -> list[Path]:
    return sorted(_pact_file().parent.glob("first_run_pact_state.json.corrupt-*"))


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0.0,) * len(windows_sharing_retry._DELAYS))
    return monkeypatch


def _deny_text_reads(monkeypatch, times: int | None, err: int = errno.EACCES) -> list[int]:
    """Refuse text opens of the pact file `times` times (None: always). Byte reads still work."""
    target = os.path.normcase(os.path.abspath(_pact_file()))
    calls = [0]
    real_open = builtins.open
    real_read_text = Path.read_text

    def refuse(path) -> None:
        if os.path.normcase(os.path.abspath(path)) == target and (times is None or calls[0] < times):
            calls[0] += 1
            cls = PermissionError if err == errno.EACCES else OSError
            raise cls(err, os.strerror(err), str(path))

    def fake_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and "b" not in mode:
            refuse(file)
        return real_open(file, mode, *args, **kwargs)

    def fake_read_text(self, *args, **kwargs):
        refuse(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(windows_sharing_retry, "open", fake_open, raising=False)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    return calls


def _deny_replaces(monkeypatch, times: int | None) -> list[int]:
    target = os.path.normcase(os.path.abspath(_pact_file()))
    calls = [0]
    real_replace = os.replace

    def fake_replace(src, dst, *args, **kwargs):
        if os.path.normcase(os.path.abspath(dst)) == target and (times is None or calls[0] < times):
            calls[0] += 1
            raise PermissionError(errno.EACCES, "Permission denied", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake_replace)
    return calls


def test_a_read_violation_that_clears_within_the_retries_reads_the_pact(pact_rig, on_windows):
    first_run_pact.seed()
    before = json.loads(_pact_file().read_bytes())
    calls = _deny_text_reads(on_windows, times=3)

    assert first_run_pact.load_state() == before
    assert calls[0] == 3
    assert _quarantined() == []


def test_a_read_violation_that_persists_is_busy_and_leaves_the_pact_in_place(pact_rig, on_windows):
    first_run_pact.seed()
    raw = _pact_file().read_bytes()
    _deny_text_reads(on_windows, times=None)

    with pytest.raises(PactFault) as caught:
        first_run_pact.hide(True)
    assert caught.value.code == first_run_pact.FAULT_LOCK_UNAVAILABLE
    assert caught.value.http_status == 409
    assert _quarantined() == []
    assert _pact_file().read_bytes() == raw  # not reseeded, not reset


def test_a_get_while_the_pact_cannot_be_read_answers_a_typed_409(pact_rig, on_windows):
    from core.web.api.service import dispatch_get

    first_run_pact.seed()
    raw = _pact_file().read_bytes()
    _deny_text_reads(on_windows, times=None)

    response = dispatch_get(path="/api/onboarding/pact", query={}, runtime=pact_rig.runtime,
                            model_name="vool", client_host="127.0.0.1")
    assert response.status == 409
    assert json.loads(response.body)["error"] == first_run_pact.FAULT_LOCK_UNAVAILABLE
    assert _quarantined() == []
    assert _pact_file().read_bytes() == raw


def test_any_os_error_reading_the_pact_on_windows_is_cant_read_not_corrupt(pact_rig, on_windows):
    first_run_pact.seed()
    raw = _pact_file().read_bytes()
    _deny_text_reads(on_windows, times=None, err=errno.EIO)

    with pytest.raises(PactFault) as caught:
        first_run_pact.load_state()
    assert caught.value.code == first_run_pact.FAULT_LOCK_UNAVAILABLE
    assert _quarantined() == []
    assert _pact_file().read_bytes() == raw


def test_a_replace_violation_that_clears_within_the_retries_publishes(pact_rig, on_windows):
    first_run_pact.seed()
    before = first_run_pact.load_state()
    calls = _deny_replaces(on_windows, times=3)

    after = first_run_pact.hide(True)
    assert calls[0] == 3
    assert after["revision"] == before["revision"] + 1
    assert json.loads(_pact_file().read_bytes())["welcome_hidden"] is True


def test_a_replace_violation_that_persists_is_busy_and_leaves_no_temp_file(pact_rig, on_windows):
    first_run_pact.seed()
    raw = _pact_file().read_bytes()
    _deny_replaces(on_windows, times=None)

    with pytest.raises(PactFault) as caught:
        first_run_pact.hide(True)
    assert caught.value.code == first_run_pact.FAULT_LOCK_UNAVAILABLE
    assert _pact_file().read_bytes() == raw
    assert list(_pact_file().parent.glob(".first_run_pact-*.tmp")) == []


def test_off_windows_a_read_permission_error_keeps_todays_behaviour(pact_rig, monkeypatch):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", False)
    first_run_pact.seed()
    _deny_text_reads(monkeypatch, times=None)

    assert first_run_pact.load_state() is None  # unchanged on POSIX: unreadable reads as corrupt
