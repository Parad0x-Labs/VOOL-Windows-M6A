"""Windows: the Blackbox journal survives a moment's sharing violation instead of failing a write.

T59 Windows full suite (run 42408115481): tests/project_archaeology/test_restart_concurrency.py::
test_concurrent_writer_appends_do_not_disturb_readers failed with PermissionError(13, 'Access is
denied') in the writer. Every append rewrites HEAD with an atomic replace, and on Windows
`os.replace` is refused while a reader holds the target open without delete sharing; a read is
refused too while a replace swaps the file in. Those handles close within moments, so the journal
now goes through core/windows_sharing_retry.py, the bounded retry the memory and liquefy stores use.
Off Windows a refusal is still raised at once. The Windows rule is driven through the helper's
`_IS_WINDOWS` flag.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

from core import windows_sharing_retry
from storage.blackbox.journal import Journal


def _entry(i: int) -> dict[str, object]:
    return {"schema": "blackbox_effect_v1", "kind": "effect_intended", "effect_id": f"eff-{i}", "turn_id": "turn-1"}


def _refuse_replace(monkeypatch, target: Path, times: int) -> list[str]:
    real = os.replace
    refused: list[str] = []

    def _replace(src, dst, *args, **kwargs):
        if len(refused) < times and Path(dst) == target:
            refused.append(str(dst))
            raise PermissionError(13, "Access is denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)
    return refused


def _refuse_read_once(monkeypatch, target: Path) -> list[str]:
    real_open = io.open
    refused: list[str] = []

    def _open(file, mode="r", *args, **kwargs):
        if not refused and "r" in mode and isinstance(file, (str, os.PathLike)) and Path(file) == target:
            refused.append(str(file))
            raise PermissionError(13, "Access is denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", _open)
    monkeypatch.setattr(builtins, "open", _open)
    return refused


def test_an_append_survives_a_head_replace_refused_for_a_moment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    journal = Journal(tmp_path / "journal")
    journal.append(_entry(0))
    refused = _refuse_replace(monkeypatch, journal.head_path, times=1)

    journal.append(_entry(1))

    assert refused, "HEAD's replace was never refused, so this proves nothing"
    assert [e["effect_id"] for e in journal.entries()] == ["eff-0", "eff-1"]
    assert journal.head()["count"] == 2
    assert journal.verify().ok


def test_reads_refused_for_a_moment_still_see_the_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", True)
    journal = Journal(tmp_path / "journal")
    journal.append(_entry(0))

    refused = _refuse_read_once(monkeypatch, journal.journal_path)
    assert [e["effect_id"] for e in journal.entries()] == ["eff-0"]
    assert refused, "the journal read was never refused, so this proves nothing"

    refused = _refuse_read_once(monkeypatch, journal.head_path)
    assert journal.head()["count"] == 1
    assert refused, "the HEAD read was never refused, so this proves nothing"


def test_off_windows_a_refused_head_replace_is_raised_at_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(windows_sharing_retry, "_IS_WINDOWS", False)
    journal = Journal(tmp_path / "journal")
    journal.append(_entry(0))
    refused = _refuse_replace(monkeypatch, journal.head_path, times=99)

    with pytest.raises(PermissionError):
        journal.append(_entry(1))
    assert len(refused) == 1
