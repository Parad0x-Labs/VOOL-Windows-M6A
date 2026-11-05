"""The digest key is 32 raw bytes on disk on every OS, whatever bytes os.urandom drew.

On Windows os.open without O_BINARY opens the file in text mode, so os.write turns every 0x0A
in the key into 0x0D 0x0A. A key holding a newline byte then lands as 33 bytes, the next read
sees a length that is not 32, and a fresh key replaces it: every keyed digest changes from one
call to the next (T34 suite 41969949799: "assert 33 == 32").
"""
from __future__ import annotations

from core.evidence_kernel import receipts

_KEY_WITH_NEWLINES = b"\n" + bytes(range(1, 30)) + b"\r\n"


def _pin_key(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "_ledger_dir", lambda: tmp_path / "receipts_v2")
    monkeypatch.setattr(receipts.os, "urandom", lambda n: _KEY_WITH_NEWLINES[:n])
    return tmp_path / "receipts_v2" / receipts._DIGEST_KEY_NAME


def test_a_key_holding_newline_bytes_is_stored_byte_for_byte(tmp_path, monkeypatch) -> None:
    assert len(_KEY_WITH_NEWLINES) == 32
    path = _pin_key(tmp_path, monkeypatch)
    assert receipts._digest_key() == _KEY_WITH_NEWLINES
    assert path.read_bytes() == _KEY_WITH_NEWLINES


def test_the_same_key_comes_back_on_the_next_call(tmp_path, monkeypatch) -> None:
    _pin_key(tmp_path, monkeypatch)
    first = receipts._digest_key()
    monkeypatch.setattr(receipts.os, "urandom", lambda n: b"\x00" * n)
    assert receipts._digest_key() == first
