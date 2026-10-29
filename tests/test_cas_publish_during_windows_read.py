"""A CAS publish that meets a Windows reader is retried, and a lost race to the same bytes is success.

On Windows, ``os.replace`` onto an object another thread or process holds open fails with
PermissionError (a sharing violation). Two publishers of one address can both see it absent; the
first rename lands, a reader opens the object, and the second rename fails although the object at
the address is already correct. A publish there is retried briefly; if it still cannot rename, an
object that verifies at the address is the published result. Anything else still raises, the temp
file is removed either way, and POSIX keeps its single rename.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from storage import cas_integrity
from storage.cas_integrity import atomic_publish

DATA = b"whole object\n"
ADDRESS = hashlib.sha256(DATA).hexdigest()


def _replaces(monkeypatch, failures: int, *, land_first: bytes | None = None) -> list[int]:
    """Make the first ``failures`` renames raise a sharing violation; return the call counter.

    ``land_first`` is written at the destination before the first failure, as another publisher's
    rename that won the race.
    """
    real = os.replace
    calls = [0]

    def replace(src, dst):
        calls[0] += 1
        if calls[0] <= failures:
            if land_first is not None and calls[0] == 1:
                Path(dst).write_bytes(land_first)
            raise PermissionError(13, "Permission denied", str(dst))
        return real(src, dst)

    monkeypatch.setattr(cas_integrity.os, "replace", replace)
    monkeypatch.setattr(cas_integrity.time, "sleep", lambda _s: None)
    return calls


def _temps(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_windows_publishes_once_the_reader_lets_go(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    calls = _replaces(monkeypatch, 2)
    atomic_publish(tmp_path / ADDRESS, DATA)
    assert (tmp_path / ADDRESS).read_bytes() == DATA
    assert calls[0] == 3
    assert _temps(tmp_path) == []


def test_windows_accepts_the_same_bytes_another_publisher_landed(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    calls = _replaces(monkeypatch, 10_000, land_first=DATA)
    atomic_publish(tmp_path / ADDRESS, DATA)
    assert (tmp_path / ADDRESS).read_bytes() == DATA
    assert 1 < calls[0] <= 50  # retried, and bounded
    assert _temps(tmp_path) == []


def test_windows_still_raises_when_the_address_never_holds_the_object(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    _replaces(monkeypatch, 10_000, land_first=b"wrong bytes\n")
    with pytest.raises(PermissionError):
        atomic_publish(tmp_path / ADDRESS, DATA)
    assert _temps(tmp_path) == []


def test_posix_keeps_its_single_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", False, raising=False)
    calls = _replaces(monkeypatch, 1, land_first=DATA)
    with pytest.raises(PermissionError):
        atomic_publish(tmp_path / ADDRESS, DATA)
    assert calls[0] == 1
    assert _temps(tmp_path) == []
