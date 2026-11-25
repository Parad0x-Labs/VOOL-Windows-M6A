"""Windows: verifying the Blackbox journal is never thrown off by an append that lands mid-read.

``Journal.verify`` reads the journal's lines and then its HEAD. An append that completes between the
two reads leaves HEAD one entry ahead of the lines read, and the chain reported "tail_truncated" with
nothing wrong. On Windows a file open is slow enough for that to happen under a live producer (the
rebuild witness row, run 43964307548). A Windows host now verifies under the journal lock; every
other host reads exactly as Mac ships it, and the off-Windows row pins that.
"""
from __future__ import annotations

import threading

import pytest

from core import windows_write_gate
from storage.blackbox.journal import Journal


def _entry(index: int) -> dict:
    return {"schema": "blackbox_effect_v1", "kind": "effect_intended", "effect_id": f"eff-{index}"}


@pytest.fixture()
def append_lands_between_the_reads(tmp_path, monkeypatch):
    """A journal of two entries whose verify, right after reading the lines, lets another thread
    append once and waits (bounded) for that append to finish before HEAD is read."""
    journal = Journal(tmp_path / "blackbox")
    journal.append(_entry(0))
    journal.append(_entry(1))
    real = Journal._raw_lines
    appended = threading.Event()
    appenders: list[threading.Thread] = []

    def lines_then_an_append(self):
        lines = real(self)
        if not appenders:
            def append():
                Journal(self.root).append(_entry(2))
                appended.set()

            worker = threading.Thread(target=append)
            appenders.append(worker)
            worker.start()
            appended.wait(2)
        return lines

    monkeypatch.setattr(Journal, "_raw_lines", lines_then_an_append)
    yield journal, appended
    for worker in appenders:
        worker.join(5)


def test_a_windows_host_verifies_under_the_lock_so_a_concurrent_append_waits(
    append_lands_between_the_reads, monkeypatch
) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    journal, appended = append_lands_between_the_reads
    report = journal.verify()
    assert (report.ok, report.reason) == (True, "verified")
    assert report.entries == 2
    assert appended.wait(5), "the append ran once the verify let go of the lock"
    assert (journal.verify().ok, len(journal.entries())) == (True, 3)


def test_off_windows_the_verify_reads_without_the_lock_as_mac_ships_it(
    append_lands_between_the_reads, monkeypatch
) -> None:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    journal, appended = append_lands_between_the_reads
    report = journal.verify()
    assert appended.is_set()
    assert (report.ok, report.reason) == (False, "tail_truncated")
