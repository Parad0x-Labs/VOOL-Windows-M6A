"""Windows: an Open after a Stop keeps the Stop marker when schtasks could not be run at all.

``rearm_after_open`` used to read any failed ``schtasks /query`` as "no VOOL_Daemon task on this
install" and clear ``.vool_stopped``. When the query never ran (``system_tool`` refuses an untrusted
``%SystemRoot%``, the start fails), the task is still disabled from the Stop, the marker was gone, and
no later Open ever tried to re-enable it: the Stop stuck for good. Only a query that ran and answered
counts as "nothing to re-arm"; a query that could not run keeps the marker for the next Open.
"""
from __future__ import annotations

import sys
import types

from core import process_liveness
from installer.vool_stop import STOPPED_MARKER, rearm_after_open


def _refuse(*_args, **_kwargs):
    raise OSError("SystemRoot is not an absolute Windows folder; Windows tools are not trusted")


def test_marker_stays_when_schtasks_cannot_be_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_liveness, "system_tool", _refuse)
    (tmp_path / STOPPED_MARKER).write_text("stopped\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append([str(c) for c in cmd])
        return types.SimpleNamespace(stdout="", returncode=0)

    result = rearm_after_open(tmp_path, run=run)
    assert calls == []
    assert (tmp_path / STOPPED_MARKER).exists(), "a query that never ran must not clear the Stop"
    assert result["watchdog_rearmed"] is False
    assert result["marker_cleared"] is False


def test_marker_stays_when_schtasks_cannot_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / STOPPED_MARKER).write_text("stopped\n", encoding="utf-8")

    def run(cmd, **_kwargs):
        raise FileNotFoundError(str(cmd[0]))

    result = rearm_after_open(tmp_path, run=run)
    assert (tmp_path / STOPPED_MARKER).exists()
    assert result["marker_cleared"] is False


def test_a_query_that_ran_and_found_no_task_still_clears_the_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(process_liveness, "system_tool", lambda *parts: "C:\\Windows\\System32\\" + parts[0] + ".exe")
    (tmp_path / STOPPED_MARKER).write_text("stopped\n", encoding="utf-8")

    def run(cmd, **_kwargs):
        return types.SimpleNamespace(stdout="", stderr="ERROR: The system cannot find the file specified.", returncode=1)

    result = rearm_after_open(tmp_path, run=run)
    assert not (tmp_path / STOPPED_MARKER).exists()
    assert result["watchdog_rearmed"] is False
