"""Windows: a liquefy replace or unlink that a sharing violation refuses for a moment is retried (sweep S13).

On Windows `os.replace` and `os.unlink` fail with PermissionError (WinError 5 or 32) while another process
holds the target open without delete sharing: a reader of the same store, an antivirus scan or the search
indexer. Those handles close within moments. The store raised on the first refusal, so a seal, a hot-log
rotation, a cold erasure or a CAS reconstruction failed, and an erasure could leave a segment on disk after
its cursor said it was gone. The Windows path is taken through `core.windows_sharing_retry._IS_WINDOWS`, so
these tests run on every host."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.liquefy_cas as liquefy
from core import windows_sharing_retry


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_sharing_retry, "_DELAYS", (0,) * 10)


@pytest.fixture()
def briefly_shared(monkeypatch):
    """Every replace or unlink is refused twice for each target, as an open handle refuses it."""
    refused: dict[str, int] = {}
    real_replace, real_unlink = os.replace, os.unlink

    def _refuse(target) -> None:
        key = os.fspath(target)
        refused[key] = refused.get(key, 0) + 1
        if refused[key] <= 2:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", key)

    def replace(src, dst, *args, **kwargs):
        _refuse(dst)
        return real_replace(src, dst, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        _refuse(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    return refused


def test_a_replace_is_retried_until_the_handle_closes(windows, briefly_shared, tmp_path):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    windows_sharing_retry.replace(src, dst)
    assert dst.read_bytes() == b"new" and not src.exists()


def test_a_replace_still_refused_after_the_bound_raises(windows, monkeypatch, tmp_path):
    calls = []

    def never(src, dst):
        calls.append(dst)
        raise PermissionError(13, "denied", os.fspath(dst))

    monkeypatch.setattr(os, "replace", never)
    with pytest.raises(PermissionError):
        windows_sharing_retry.replace(tmp_path / "a.tmp", tmp_path / "a")
    assert len(calls) == len(windows_sharing_retry._DELAYS) + 1


def test_off_windows_a_refused_replace_raises_at_once(monkeypatch, briefly_shared, tmp_path):
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", False)
    src = tmp_path / "a.tmp"
    src.write_bytes(b"new")
    with pytest.raises(PermissionError):
        windows_sharing_retry.replace(src, tmp_path / "a")
    assert briefly_shared == {os.fspath(tmp_path / "a"): 1}


def test_an_unlink_is_retried_and_a_missing_file_is_allowed_when_asked(windows, briefly_shared, tmp_path):
    target = tmp_path / "gone"
    target.write_bytes(b"x")
    windows_sharing_retry.unlink(target)
    assert not target.exists()
    windows_sharing_retry.unlink(tmp_path / "never-there", missing_ok=True)
    with pytest.raises(FileNotFoundError):
        windows_sharing_retry.unlink(tmp_path / "never-there-either")


def test_a_seal_and_hot_rotation_survive_a_brief_sharing_violation(windows, briefly_shared, make_store, sample_events):
    store = make_store()
    store.append(sample_events(40))
    sealed = store.seal()
    assert sealed is not None and sealed.event_count == 40
    reopened = make_store(root=store.root)
    assert reopened.read(7)["ref"]["event_id"] == "effect-0006"


def test_a_cold_erasure_removes_its_segments_through_a_brief_sharing_violation(
    windows, briefly_shared, make_store, sample_events
):
    store = make_store()
    store.append(sample_events(40))
    store.seal()
    store.append(sample_events(40, start=40))
    store.seal()
    removed = store.delete_cold_before(41)
    assert removed
    on_disk = {path.stem for path in store.segments_dir.glob("*.lsegh")}
    assert not on_disk & set(removed)


def test_a_cas_reconstruction_publishes_through_a_brief_sharing_violation(windows, tmp_path, monkeypatch):
    monkeypatch.setattr(liquefy, "CAS_DIR", tmp_path / "cas")
    monkeypatch.setattr(liquefy, "CHUNK_SIZE", 1024)
    source = tmp_path / "source.bin"
    source.write_bytes(os.urandom(3000))
    manifest = liquefy.chunk_file(str(source))
    out = tmp_path / "restored.bin"
    out.write_bytes(b"an editor still has this open")
    refused = {"n": 0}
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(dst) == out and refused["n"] < 2:
            refused["n"] += 1
            raise PermissionError(13, "in use", os.fspath(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    liquefy.reconstruct_object(manifest["root_hash"], str(out))
    assert out.read_bytes() == source.read_bytes()
