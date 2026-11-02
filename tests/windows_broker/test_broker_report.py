"""What VOOL can show about the Windows broker (Phase C slice C5, picks 6 and 7).

The host keeps the start line's journal state: whether undo records can be written (`available`),
the batches still pending (each with what is left out of place), and torn last records (each kept in
a `.torn` file), plus the entries the broker skipped. `session.report()` gives that as one plain
dict for the API and the "Check and clear" banner. A journal that is missing or malformed on the start
line reads as unavailable (unknown fails closed), and a process that never started a broker reports
that it is not wired.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.windows_broker import session
from core.windows_broker.process import BrokerHost, BrokerStatus

from .test_one_flagged_list import _Job, _Process

FLAGGED = [{"id": "a" * 48, "path": "C:\\work\\a"}]


def _host(tmp_path: Path, **line: Any) -> BrokerHost:
    ready = {"pipe": r"\\.\pipe\vool-broker-test", "broker_pid": 4242, "flagged": FLAGGED, **line}
    encoded = json.dumps(ready).encode() + b"\n"
    exe = tmp_path / "VoolBroker.exe"
    exe.write_bytes(b"")
    host = BrokerHost(exe, [], tmp_path / "journal", popen=lambda argv, **options: _Process(encoded),
                      job_factory=_Job, api_identity=lambda: (1, 2), timeout=5.0)
    host.start()
    return host


@pytest.fixture(autouse=True)
def _no_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_host", None)


def test_the_host_keeps_the_journal_state_and_the_skipped_entries(tmp_path: Path) -> None:
    journal = {"available": True, "recovered": 1,
               "pending": [{"batch_id": "b1", "left": ["x.txt"]}],
               "torn": [{"batch_id": "b2", "bytes": 17, "kept_in": "batch-b2.torn"}]}
    skipped = [{"entry": "D:\\share", "code": "unsupported_filesystem", "message": "network"}]
    host = _host(tmp_path, journal=journal, skipped=skipped)
    session.register(host)
    report = session.report()
    assert report == {
        "wired": True, "running": True, "reason": "",
        "flagged": [{"id": "a" * 48, "path": "C:\\work\\a"}],
        "skipped": skipped,
        "undo_records": "available",
        "pending": [{"batch_id": "b1", "left": ["x.txt"]}],
        "torn": [{"batch_id": "b2", "bytes": 17, "kept_in": "batch-b2.torn"}],
        "needs_check": True,
    }


def test_a_clean_journal_needs_no_check(tmp_path: Path) -> None:
    session.register(_host(tmp_path, journal={"available": True, "pending": [], "torn": []}))
    report = session.report()
    assert report["undo_records"] == "available" and report["needs_check"] is False


@pytest.mark.parametrize("journal", [None, "yes", {"available": False}, {"pending": []}, {"available": "true"}])
def test_a_journal_that_is_unavailable_or_unclear_reads_as_unavailable(tmp_path: Path, journal: Any) -> None:
    line = {} if journal is None else {"journal": journal}
    session.register(_host(tmp_path, **line))
    report = session.report()
    assert report["running"] is True
    assert report["undo_records"] == "unavailable"


def test_malformed_pending_and_torn_rows_are_kept_as_unknown_rows(tmp_path: Path) -> None:
    session.register(_host(tmp_path, journal={"available": True, "pending": ["b1", {"batch_id": 3}], "torn": [7]}))
    report = session.report()
    assert report["pending"] == [{"batch_id": "unknown", "left": []}, {"batch_id": "unknown", "left": []}]
    assert report["torn"] == [{"batch_id": "unknown", "bytes": 0, "kept_in": ""}]
    assert report["needs_check"] is True


def test_a_broker_that_is_not_running_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Host:
        status = BrokerStatus(False, "refused:reparse_point")

    session.register(_Host())  # type: ignore[arg-type]
    report = session.report()
    assert report["wired"] is True and report["running"] is False and report["reason"] == "refused:reparse_point"
    assert report["undo_records"] == "unavailable" and report["pending"] == [] and report["needs_check"] is False


def test_a_process_that_never_started_a_broker_is_not_wired() -> None:
    assert session.report() == {"wired": False}
