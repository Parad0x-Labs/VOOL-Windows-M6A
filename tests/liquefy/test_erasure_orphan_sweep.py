"""An erased cold segment that a refused unlink left on disk is receipted and swept, never stranded.

delete_cold_before drops the segments from the cursor first and unlinks their files second. When an
unlink was still refused after the Windows sharing-violation retries (an antivirus scan or a reader
holding the file), the call raised before the erasure receipt was written and skipped the rest of
the batch. The cursor no longer listed those segments, so a repeat call returned [] and the erased
bytes stayed on disk with no receipt. Now every file in the batch is tried, the receipt names the
files still on disk, the call fails closed, and a later erasure or the next open removes them. Only
a segment that an erasure receipt names and the cursor on disk no longer lists is ever swept."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import windows_sharing_retry
from core.liquefy.store import EntryNotFound, LiquefyStoreError


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0,) * 10)


@pytest.fixture()
def held_open(monkeypatch):
    """Unlinks of the paths in the returned set are refused, as a handle without delete sharing
    refuses them; discard a path to close its handle. Every other path passes straight through."""
    held: set[str] = set()
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if os.path.abspath(os.fspath(path)) in held:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    return held


def _three_sealed(make_store, sample_events):
    store = make_store()
    for start in (0, 20, 40):
        store.append(sample_events(20, start=start))
        store.seal()
    first, second, third = sorted(store.segments_dir.glob("*.lsegh"))
    return store, first, second, third


def _receipts(store) -> list[dict]:
    path = store.receipts_dir / "erasure.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_refused_unlink_still_tries_the_rest_receipts_what_is_left_and_fails_closed(
    windows, held_open, make_store, sample_events
):
    store, first, second, _ = _three_sealed(make_store, sample_events)
    held_open.add(os.path.abspath(first))

    with pytest.raises(LiquefyStoreError):
        store.delete_cold_before(41)

    assert first.exists() and not second.exists()
    receipts = _receipts(store)
    assert len(receipts) == 1
    assert receipts[0]["segment_ids"] == [first.stem, second.stem]
    assert receipts[0]["unlink_pending"] == [first.stem]


def test_a_repeat_erasure_removes_the_left_file_once_its_handle_closes(
    windows, held_open, make_store, sample_events
):
    store, first, second, _ = _three_sealed(make_store, sample_events)
    held_open.add(os.path.abspath(first))
    with pytest.raises(LiquefyStoreError):
        store.delete_cold_before(41)
    held_open.clear()

    swept = store.delete_cold_before(41)

    assert swept == [first.stem]
    assert not first.exists()
    last = _receipts(store)[-1]
    assert last["action"] == "erase_swept" and last["segment_ids"] == [first.stem]
    assert store.delete_cold_before(41) == []


def test_reopening_the_store_removes_a_left_erased_file(windows, held_open, make_store, sample_events):
    store, first, _, third = _three_sealed(make_store, sample_events)
    held_open.add(os.path.abspath(first))
    with pytest.raises(LiquefyStoreError):
        store.delete_cold_before(41)
    held_open.clear()

    reopened = make_store(root=store.root)

    assert not first.exists() and third.exists()
    assert _receipts(reopened)[-1] == {**_receipts(reopened)[-1], "action": "erase_swept", "segment_ids": [first.stem]}
    assert reopened.read(41)["ref"]["event_id"] == "effect-0040"
    assert reopened.verify(deep=True)["ok"] is True


def test_a_file_still_held_at_reopen_stays_pending_and_the_store_still_opens(
    windows, held_open, make_store, sample_events
):
    store, first, _, _ = _three_sealed(make_store, sample_events)
    held_open.add(os.path.abspath(first))
    with pytest.raises(LiquefyStoreError):
        store.delete_cold_before(41)

    reopened = make_store(root=store.root)

    assert first.exists()
    assert reopened.read(41)["ref"]["event_id"] == "effect-0040"
    assert [row["action"] for row in _receipts(reopened)] == ["erase_cold_before"]


def test_a_segment_file_no_erasure_names_is_never_swept(make_store, sample_events):
    store, first, _, _ = _three_sealed(make_store, sample_events)
    stray = store.segments_dir / "000000009001-000000009010-abcdefabcdef.lsegh"
    stray.write_bytes(first.read_bytes())  # e.g. a seal that crashed before its cursor write

    make_store(root=store.root).delete_cold_before(21)

    assert stray.exists()


def test_a_segment_the_cursor_on_disk_still_lists_is_never_swept(make_store, sample_events):
    store, first, second, _ = _three_sealed(make_store, sample_events)
    (store.receipts_dir / "erasure.jsonl").write_text(
        json.dumps({"action": "erase_cold_before", "segment_ids": [second.stem]}) + "\n", encoding="utf-8"
    )

    reopened = make_store(root=store.root)
    reopened.delete_cold_before(21)

    assert second.exists() and not first.exists()
    assert reopened.read(21)["ref"]["event_id"] == "effect-0020"


def test_nothing_is_swept_while_the_cursor_on_disk_cannot_be_read(
    windows, held_open, make_store, sample_events
):
    store, first, _, _ = _three_sealed(make_store, sample_events)
    held_open.add(os.path.abspath(first))
    with pytest.raises(LiquefyStoreError):
        store.delete_cold_before(41)
    held_open.clear()
    Path(store.cursor_path).write_bytes(b"{not json")

    store.delete_cold_before(41)

    assert first.exists()


@pytest.mark.parametrize("named", ["../victim", "000000000001-000000000020-90f3db55334f/../../victim", "VICTIM"])
def test_a_receipt_name_that_is_not_a_segment_id_is_never_a_path_to_remove(make_store, sample_events, named):
    store, _, _, _ = _three_sealed(make_store, sample_events)
    victim = store.root / "victim"
    victim.write_bytes(b"keep me")
    (store.segments_dir / "VICTIM.lsegh").write_bytes(b"keep me too")
    (store.receipts_dir / "erasure.jsonl").write_text(
        json.dumps({"action": "erase_cold_before", "segment_ids": [named]}) + "\n", encoding="utf-8"
    )

    make_store(root=store.root).delete_cold_before(1)

    assert victim.read_bytes() == b"keep me"
    assert (store.segments_dir / "VICTIM.lsegh").read_bytes() == b"keep me too"


def test_seq_never_goes_backwards_after_every_segment_is_erased(make_store, sample_events):
    store, _, _, _ = _three_sealed(make_store, sample_events)

    store.delete_cold_before(10_000)
    first_new = store.append(sample_events(1, start=900))
    reopened = make_store(root=store.root)
    after_reopen = reopened.append(sample_events(1, start=901))

    assert first_new == [61] and after_reopen == [62]
    with pytest.raises(EntryNotFound):
        reopened.read(5)
