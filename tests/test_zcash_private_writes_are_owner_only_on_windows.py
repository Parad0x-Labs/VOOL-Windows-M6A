"""On Windows a Zcash private write is made owner-only before any byte lands, or nothing is saved.

mkstemp's 0600 only clears the read-only bit on Windows, so write_private's "owner-only (0600)"
held there only when the folder's inherited DACL happened to be private. These tests drive the
Windows branch on any OS by setting the module flag and standing in for core.owner_only.
"""
from __future__ import annotations

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyUnavailable
from core.zcash import config
from core.zcash.watch import ZcashWatchError, write_private


def test_the_temp_is_protected_while_still_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "_WINDOWS", True)
    seen = []
    monkeypatch.setattr(owner_only, "require_owner_only", lambda path, *, directory=False: seen.append(path.read_bytes()))
    target = tmp_path / "state.json"
    write_private(target, '{"i": 1}')
    assert seen == [b""]
    assert target.read_text(encoding="utf-8") == '{"i": 1}'


def test_nothing_is_saved_when_owner_only_is_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "_WINDOWS", True)

    def _refuse(path, **_kwargs):
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(owner_only, "require_owner_only", _refuse)
    target = tmp_path / "state.json"
    target.write_text("previous", encoding="utf-8")
    with pytest.raises(ZcashWatchError) as refused:
        write_private(target, "new")
    assert refused.value.code == "data_dir_unsafe"
    assert target.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_posix_never_consults_owner_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "_WINDOWS", False)
    monkeypatch.setattr(owner_only, "require_owner_only", lambda *a, **k: pytest.fail("consulted off Windows"))
    target = tmp_path / "state.json"
    write_private(target, "x")
    assert target.read_text(encoding="utf-8") == "x"
