"""Quarantining a corrupt bypass-grants store must never collide and never fail open."""

from __future__ import annotations

import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import mode_permission_policy as policy


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "authority" / "bypass_grants.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(policy, "_bypass_grants_path", lambda: path)
    return path


def _refused_in(folder: Path, real):
    """`real`, except for a path inside ``folder``, which it refuses. The fakes replace calls for the
    whole process, so anything outside the store's folder (the CI watchdog's progress file, another
    thread's state) must pass straight through."""

    def call(target, *args, **kwargs):
        if Path(target).resolve().is_relative_to(folder.resolve()):
            raise OSError("cannot move (fixture)")
        return real(target, *args, **kwargs)

    return call


def _restore_corrupt(path: Path) -> int:
    path.write_text("{not json", encoding="utf-8")
    policy._BYPASS_GRANTS.clear()
    policy._BYPASS_GRANTS_RESTORED = False
    return policy.restore_persisted_bypass_grants()


def test_two_corrupt_stores_quarantined_in_one_second_get_distinct_names(store, monkeypatch):
    # Freeze the whole-second clock: the old `.corrupt-<int(time.time())>` suffix then repeats.
    monkeypatch.setattr(
        policy, "time", SimpleNamespace(time=lambda: 1_700_000_000.0, time_ns=_real_time.time_ns)
    )

    assert _restore_corrupt(store) == 0
    assert not store.exists(), "first corrupt file stayed live"
    assert _restore_corrupt(store) == 0
    assert not store.exists(), "second corrupt file stayed live (suffix collision swallowed)"

    quarantined = list(store.parent.glob("bypass_grants.json.corrupt-*"))
    assert len(quarantined) == 2
    assert len({p.name for p in quarantined}) == 2


def test_unmovable_corrupt_store_is_unlinked_not_left_live(store, monkeypatch):
    monkeypatch.setattr(policy.os, "replace", _refused_in(store.parent, policy.os.replace))
    monkeypatch.setattr(Path, "rename", _refused_in(store.parent, Path.rename))

    assert _restore_corrupt(store) == 0
    assert not store.exists(), "corrupt file survived a failed quarantine"


def test_corrupt_store_that_can_be_neither_moved_nor_removed_raises(store, monkeypatch):
    monkeypatch.setattr(policy.os, "replace", _refused_in(store.parent, policy.os.replace))
    monkeypatch.setattr(Path, "rename", _refused_in(store.parent, Path.rename))
    monkeypatch.setattr(Path, "unlink", _refused_in(store.parent, Path.unlink))

    with pytest.raises(policy.BypassStoreError):
        _restore_corrupt(store)
    assert not policy._BYPASS_GRANTS, "nothing may be restored from a store that could not be quarantined"
