"""On Windows the Zcash folders and invoice store are owner-only through core/owner_only, or refused.

``os.chmod`` only flips the read-only bit there, so the data root, the per-network folder, the
pinned-tool folder and the wallet folder are created with ``ensure_private_dir`` (files the watch
tool writes inside inherit owner-only), entries already in the wallet folder are secured by handle,
and the invoice store is put through ``require_owner_only`` before it is opened. When that cannot be
reached nothing runs. These tests drive the Windows branch on any host by setting ``_WINDOWS``.
"""
from __future__ import annotations

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyReport, OwnerOnlyUnavailable
from core.zcash import config, watch
from core.zcash.invoices import InvoiceStore


def _refuse(path, **_kwargs):
    raise OwnerOnlyUnavailable(path, "test_refusal")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(config, "_WINDOWS", True)


def _private(calls):
    def _ensure(path, *, fresh=False):
        calls.append(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _ensure


def test_the_data_folders_are_created_owner_only(tmp_path, monkeypatch, windows):
    calls = []
    monkeypatch.setattr(owner_only, "ensure_private_dir", _private(calls))
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "zcash"))
    folder = config.data_dir()
    assert calls == [tmp_path / "zcash", folder]
    assert folder.parent == tmp_path / "zcash"


def test_a_wallet_folder_that_cannot_be_owner_only_runs_nothing(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(owner_only, "ensure_private_dir", _refuse)
    with pytest.raises(watch.ZcashWatchError) as unsafe:
        watch._private_dir(tmp_path / "wallet")
    assert unsafe.value.code == "data_dir_unsafe"


def test_entries_already_in_the_wallet_folder_are_secured_and_a_failure_runs_nothing(tmp_path, monkeypatch, windows):
    (tmp_path / "data.sqlite").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    seen = []

    def _make(path, *, directory=False):
        seen.append((path.name, directory))
        return OwnerOnlyReport(True, "")

    monkeypatch.setattr(owner_only, "make_owner_only", _make)
    watch._tighten(tmp_path)
    assert sorted(seen) == [("data.sqlite", False), ("sub", True)]

    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, *, directory=False: OwnerOnlyReport(False, "extra_ace"))
    with pytest.raises(watch.ZcashWatchError) as unsafe:
        watch._tighten(tmp_path)
    assert unsafe.value.code == "data_dir_unsafe"


def test_a_wallet_folder_that_cannot_be_listed_runs_nothing(tmp_path, monkeypatch, windows):
    missing = tmp_path / "gone"  # iterdir raises FileNotFoundError, an OSError
    with pytest.raises(watch.ZcashWatchError) as unavailable:
        watch._tighten(missing)
    assert unavailable.value.code == "data_dir_unavailable"


def test_the_invoice_store_is_not_opened_unprotected(tmp_path, monkeypatch, windows):
    monkeypatch.setattr(owner_only, "require_owner_only", _refuse)
    store = InvoiceStore.__new__(InvoiceStore)
    store.path = tmp_path / "invoices.sqlite"
    with pytest.raises(OwnerOnlyUnavailable):
        with store._conn():
            pass
    assert store.path.read_bytes() == b""  # created empty, owner-only from the first byte or not opened
