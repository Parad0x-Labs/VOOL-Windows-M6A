"""The liquefy store writes the bytes it hashed, also under Windows' text-mode os.open."""
from __future__ import annotations

import pytest

from tests import _windows_text_mode_rules


@pytest.fixture(autouse=True)
def _windows_text_mode(monkeypatch):
    _windows_text_mode_rules.apply(monkeypatch)


def test_a_sealed_segment_reads_back_and_reopens(make_store, sample_events):
    store = make_store()
    store.append(sample_events(40))
    sealed = store.seal()
    assert sealed is not None and sealed.event_count == 40
    store.append(sample_events(5, session="sess-2"))
    reopened = make_store(root=store.root)
    assert reopened.read(7)["ref"]["event_id"] == "effect-0006"
    assert reopened.read(43)["session_id"] == "sess-2"


def test_no_store_file_gains_a_carriage_return(make_store, sample_events):
    store = make_store()
    store.append(sample_events(40))
    store.seal()
    store.append(sample_events(5, session="sess-2"))
    written = [path for path in store.root.rglob("*") if path.is_file()]
    assert written
    for path in written:
        if path.suffix != ".lsegh":
            assert b"\r" not in path.read_bytes(), path.name
