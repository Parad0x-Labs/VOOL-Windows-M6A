"""A CAS read that meets Windows' rename window is retried, not reported as corruption.

On Windows, opening an object's path while ``atomic_publish``'s ``os.replace`` moves the temp file
onto it fails with PermissionError (Errno 13, a sharing violation). Measured on a Windows host: 147
of a reader's verified reads during 400 publishes raised CasCorruptionError(reason="unreadable") for
objects that were whole a moment later. A read there is retried briefly; a path that stays
unreadable is still corruption (fail closed), and POSIX keeps its single read.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from storage import cas_integrity
from storage.cas_integrity import CasCorruptionError, CasMissingError, verified_read

DATA = b"whole object\n"
ADDRESS = hashlib.sha256(DATA).hexdigest()


def _reads(monkeypatch, path: Path, outcomes: list[object]) -> list[int]:
    """Make reads of ``path`` raise or return ``outcomes`` in order; return the call counter."""
    real = Path.read_bytes
    calls = [0]

    def read_bytes(self: Path) -> bytes:
        if self != path:
            return real(self)
        outcome = outcomes[min(calls[0], len(outcomes) - 1)]
        calls[0] += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(cas_integrity.time, "sleep", lambda _s: None)
    return calls


def _sharing_violation(path: Path) -> PermissionError:
    return PermissionError(13, "Permission denied", str(path))


def test_windows_reads_the_object_once_the_rename_window_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    path = tmp_path / ADDRESS
    calls = _reads(monkeypatch, path, [_sharing_violation(path), _sharing_violation(path), DATA])
    assert verified_read(path, ADDRESS) == DATA
    assert calls[0] == 3


def test_windows_reports_a_miss_when_the_object_is_gone_after_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    path = tmp_path / ADDRESS
    _reads(monkeypatch, path, [_sharing_violation(path), FileNotFoundError(2, "missing", str(path))])
    with pytest.raises(CasMissingError):
        verified_read(path, ADDRESS)


def test_windows_still_fails_closed_when_the_object_stays_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", True, raising=False)
    path = tmp_path / ADDRESS
    calls = _reads(monkeypatch, path, [_sharing_violation(path)])
    with pytest.raises(CasCorruptionError) as excinfo:
        verified_read(path, ADDRESS)
    assert excinfo.value.reason == "unreadable"
    assert 1 < calls[0] <= 50  # retried, and bounded


def test_posix_keeps_its_single_read(tmp_path, monkeypatch):
    monkeypatch.setattr(cas_integrity, "_WINDOWS", False, raising=False)
    path = tmp_path / ADDRESS
    calls = _reads(monkeypatch, path, [_sharing_violation(path), DATA])
    with pytest.raises(CasCorruptionError) as excinfo:
        verified_read(path, ADDRESS)
    assert excinfo.value.reason == "unreadable"
    assert calls[0] == 1
