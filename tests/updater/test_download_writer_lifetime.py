"""Mismatch cleanup owns a closed writer on every host, including Windows."""
from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from core.updater import download
from core.updater.download import DownloadReason

from .helpers import generate_publisher_keypair
from .test_download_resume import BLOB, FakeServer, _artifact, _downloader


@pytest.mark.parametrize("declared_size", (600, 899))
@pytest.mark.parametrize("resume", (False, True))
def test_overshoot_closes_the_writer_before_deleting_staged_bytes(tmp_path, monkeypatch, declared_size, resume):
    private_hex, public_hex = generate_publisher_keypair()
    artifact = _artifact(BLOB[:declared_size], private_hex=private_hex)
    server = FakeServer(BLOB)
    part = tmp_path / "artifact.part"
    meta = tmp_path / "artifact.part.meta.json"
    writers = []
    cleanup = []
    discarded = []
    real_open = builtins.open
    real_unlink = Path.unlink

    def track_writer(file, mode="r", *args, **kwargs):
        stream = real_open(file, mode, *args, **kwargs)
        if Path(file) == part and mode in {"wb", "ab"}:
            writers.append(stream)
        return stream

    def require_closed_writer(path, *args, **kwargs):
        if path in (part, meta) and writers:
            closed = all(writer.closed for writer in writers)
            cleanup.append((path.name, closed))
            if not closed:
                # Model Windows sharing semantics on POSIX as well: do not let
                # permissive unlink semantics hide a writer-ownership violation.
                raise PermissionError("staged artifact writer is still open")
            if path == part and path.exists():
                discarded.append(path.read_bytes())
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(download, "open", track_writer, raising=False)
    monkeypatch.setattr(Path, "unlink", require_closed_writer)

    if resume:
        server.raise_after(128)
        partial = _downloader(server).download(artifact, tmp_path, public_hex)
        assert not partial.ok and partial.resumable
        assert part.read_bytes() == BLOB[:128]
        assert all(writer.closed for writer in writers)
        writers.clear()
        server.raise_after(None)

    result = _downloader(server).download(artifact, tmp_path, public_hex)

    assert result.ok is False
    assert result.reason is DownloadReason.SIZE_MISMATCH
    assert result.path is None and result.resumable is False
    assert len(writers) == 1 and writers[0].closed
    assert writers[0].mode == ("ab" if resume else "wb")
    assert cleanup == [("artifact.part", True), ("artifact.part.meta.json", True)]
    assert discarded == [BLOB[:declared_size]]
    assert not part.exists() and not meta.exists()
    assert server.hits == (2 if resume else 1)
    assert server.range_requests == (["bytes=128-"] if resume else [])
