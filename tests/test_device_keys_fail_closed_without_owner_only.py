"""The Device Link authority key and the mobile companion state are written owner-only or not at all.

On Windows ``os.chmod`` only flips the read-only bit, so these writers go through
``core/owner_only``. When owner-only protection cannot be reached they must raise and leave no key
or state behind, never fall back to an ordinary readable file.
"""
from __future__ import annotations

import pytest

from core.device_link import identity as device_identity
from core.owner_only import OwnerOnlyUnavailable
from core.web.api import mobile_companion_api as mca


def _refuse(path, *, directory=False):
    raise OwnerOnlyUnavailable(path, "test_refusal")


def test_the_device_key_is_not_written_when_owner_only_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(device_identity, "require_owner_only", _refuse)
    key_path = tmp_path / "keys" / "auth.json"
    with pytest.raises(OwnerOnlyUnavailable):
        device_identity.load_or_create(key_path)
    assert not key_path.exists()
    assert not key_path.with_suffix(".tmp").exists()


def test_the_device_key_file_is_protected_before_the_seed_lands_in_it(tmp_path, monkeypatch):
    seen = []

    def _spy(path, *, directory=False):
        seen.append(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(device_identity, "require_owner_only", _spy)
    key_path = tmp_path / "keys" / "auth.json"
    device_identity.save(device_identity.generate(), key_path)
    assert seen == [""]
    assert "seed_b64" in key_path.read_text(encoding="utf-8")


def test_the_mobile_state_is_not_written_when_owner_only_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(mca, "require_owner_only", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        mca.CompanionStore(tmp_path)
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "state.tmp").exists()


def test_the_mobile_state_is_protected_before_any_state_lands_in_it(tmp_path, monkeypatch):
    seen = []

    def _spy(path, *, directory=False):
        seen.append(path.read_text())

    monkeypatch.setattr(mca, "require_owner_only", _spy)
    mca.CompanionStore(tmp_path)
    assert seen == [""]
    assert (tmp_path / "state.json").exists()
