"""A spent work deadline reads as spent even when the clock has not ticked since the scan began.

Windows suite 41969949799 (5 Nov 2026) failed test_local_scan.py::test_work_deadline_returns_incomplete_not_clean:
a scan given no time at all (deadline 0.0) came back ``clean``. Windows' monotonic clock moves in steps of
about 15.6 ms, so a short scan reads the same instant it started at, and a check that the clock is strictly
past the deadline never fires. A deadline the clock has reached is spent, so every check stops there.
"""
from __future__ import annotations

import pytest

from core import local_scan
from core.local_scan import scan_bytes

HEADER = b"---\nname: fixture\ndescription: Fixture skill\n---\n\n"


@pytest.fixture()
def frozen_clock(monkeypatch):
    """A clock that never ticks during the scan, as Windows' does across a short one."""
    monkeypatch.setattr(local_scan.time, "monotonic", lambda: 1000.0)


def test_no_time_at_all_is_incomplete_on_a_clock_that_has_not_ticked(frozen_clock) -> None:
    report = scan_bytes("fixture", HEADER + b"Ordinary guidance.\n", deadline=0.0)

    assert report["status"] == "incomplete"
    assert report["reason"] == "work_deadline_exceeded"
    assert report["limitations"]


def test_time_left_on_a_clock_that_has_not_ticked_still_scans(frozen_clock) -> None:
    report = scan_bytes("fixture", HEADER + b"Ordinary guidance.\n", deadline=5.0)

    assert report["status"] == "clean"


def test_the_encoded_blob_pass_stops_at_a_reached_deadline(frozen_clock) -> None:
    blob = "aGVsbG8gd29ybGQgdGhpcyBpcyBqdXN0IHByb3NlIGFuZCBub3RoaW5nIGVsc2UgYXQgYWxs"
    out, note = local_scan._encoded_payloads([blob], [blob], stop=1000.0)

    assert (out, note) == ([], "work_deadline_exceeded")
