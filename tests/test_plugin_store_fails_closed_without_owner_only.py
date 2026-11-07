"""The plugin lifecycle store is written owner-only or not at all.

On Windows ``mkstemp``'s file inherits the folder's ACL and ``os.chmod`` only flips the read-only
bit, so the store goes through ``core/owner_only``. When owner-only protection cannot be reached,
no store goes live and no temp file is left behind.
"""
from __future__ import annotations

import pytest

from core import plugin_lifecycle as lifecycle
from core.owner_only import OwnerOnlyUnavailable
from tests._toolchain_fixtures import PLUGIN_ID, make_plugin, reset_toolchain_state


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_PLUGIN_LIFECYCLE_PATH", str(tmp_path / "store" / "lifecycle.json"))
    monkeypatch.setenv("VOOL_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    plugin_dir = make_plugin(tmp_path, admit=False)
    reset_toolchain_state()
    yield plugin_dir
    reset_toolchain_state()


def test_no_store_goes_live_when_owner_only_is_unavailable(pack, monkeypatch):
    def _refuse(path, *, directory=False):
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(lifecycle, "require_owner_only", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        lifecycle.install(PLUGIN_ID, root=pack)
    store = lifecycle.store_path()
    assert not store.exists()
    assert [p.name for p in store.parent.iterdir()] == []


def test_the_store_is_protected_before_any_row_lands_in_it(pack, monkeypatch):
    seen = []

    def _spy(path, *, directory=False):
        with open(path, encoding="utf-8") as fh:
            seen.append(fh.read())

    monkeypatch.setattr(lifecycle, "require_owner_only", _spy)
    lifecycle.install(PLUGIN_ID, root=pack)
    assert seen and all(text == "" for text in seen)
    assert PLUGIN_ID in lifecycle.store_path().read_text(encoding="utf-8")
