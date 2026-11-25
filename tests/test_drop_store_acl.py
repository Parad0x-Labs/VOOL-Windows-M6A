"""Windows ACL branch of the Drop session store, driven on any OS through store._WINDOWS_ACL.

core.owner_only is replaced by recording fakes, so these pin the store's policy (create owner-only,
verify an existing folder or sidecar without repairing it, refuse junctions and other reparse points
independently of the ACL reader, map every refusal to a bounded code, fail closed, never consult mode
bits), not the Win32 calls themselves.
"""

from __future__ import annotations

import os
import secrets
import stat
import types

import pytest

from core import owner_only
from core.drop import store
from core.drop.session import DropError
from core.drop.store import EncryptedStore
from core.owner_only import OwnerOnlyReport, OwnerOnlyUnavailable


class _FakeAcl:
    def __init__(self):
        self.ensure_reason = ""
        self.verify_reason = ""
        self.make_reason = ""
        self.verify_by_name = {}
        self.make_by_name = {}
        self.calls = []

    def ensure_private_dir(self, path, *, fresh=False):
        self.calls.append(("ensure", os.path.basename(os.fspath(path)), fresh))
        if self.ensure_reason:
            raise OwnerOnlyUnavailable(path, self.ensure_reason)
        try:
            os.mkdir(path)
        except FileExistsError:
            raise OwnerOnlyUnavailable(path, "already_exists") from None
        return path

    def verify_owner_only(self, path):
        name = os.path.basename(os.fspath(path))
        self.calls.append(("verify", name))
        reason = self.verify_by_name.get(name, self.verify_reason)
        return OwnerOnlyReport(not reason, reason)

    def make_owner_only(self, path, *, directory=False):
        name = os.path.basename(os.fspath(path))
        self.calls.append(("make", name))
        reason = self.make_by_name.get(name, self.make_reason)
        return OwnerOnlyReport(not reason, reason)


@pytest.fixture
def acl(tmp_path, monkeypatch):
    # tmp_path is requested first so it exists before os.getuid is removed below.
    fake = _FakeAcl()
    monkeypatch.setattr(store, "_WINDOWS_ACL", True)
    monkeypatch.setattr(owner_only, "ensure_private_dir", fake.ensure_private_dir)
    monkeypatch.setattr(owner_only, "verify_owner_only", fake.verify_owner_only)
    monkeypatch.setattr(owner_only, "make_owner_only", fake.make_owner_only)
    # The ACL branch must never touch the POSIX-only uid or O_NOFOLLOW.
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    return fake


def _report_reparse_point(monkeypatch, target):
    # What Windows lstat reports for a directory junction: the real kind bits plus the reparse attribute.
    real = store._lstat

    def lstat(path):
        info = real(path)
        if os.fspath(path) != os.fspath(target):
            return info
        return types.SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                     st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)

    monkeypatch.setattr(store, "_lstat", lstat)


def test_store_is_created_owner_only_and_an_existing_one_is_only_verified(tmp_path, acl):
    key = secrets.token_bytes(32)
    EncryptedStore(tmp_path / "vault", key).close()
    assert ("ensure", "vault", True) in acl.calls
    assert ("verify", "vault") in acl.calls
    assert ("make", "drop.sqlite") in acl.calls
    acl.calls.clear()
    EncryptedStore(tmp_path / "vault", key).close()
    assert ("verify", "vault") in acl.calls and ("verify", "drop.sqlite") in acl.calls
    assert ("make", "vault") not in acl.calls and ("make", "drop.sqlite") not in acl.calls


@pytest.mark.parametrize("reason, code", [
    ("dacl_not_protected", "UNSAFE_STORE_PERMISSIONS"), ("foreign_ace", "UNSAFE_STORE_PERMISSIONS"),
    ("owner_not_user", "UNSAFE_STORE_PERMISSIONS"), ("null_dacl", "UNSAFE_STORE_PERMISSIONS"),
    ("empty_dacl", "UNSAFE_STORE_PERMISSIONS"), ("reparse_point", "UNSAFE_STORE_PATH"),
    ("non_local_path", "UNSAFE_STORE_PATH"), ("kind_mismatch", "UNSAFE_STORE_PATH"),
    ("a_reason_added_later", "UNSAFE_STORE_PERMISSIONS"),
])
def test_existing_folder_refusals_map_to_bounded_codes_without_repair(tmp_path, acl, reason, code):
    (tmp_path / "existing").mkdir()
    acl.verify_reason = reason
    with pytest.raises(DropError, match=f"^{code}$"):
        store.secure_directory(tmp_path / "existing")
    assert not any(call[0] == "make" for call in acl.calls)


@pytest.mark.parametrize("reason, code", [
    ("non_local_path", "UNSAFE_STORE_PATH"), ("reparse_point", "UNSAFE_STORE_PATH"),
    ("acl_write_failed", "UNSAFE_STORE_PERMISSIONS"), ("a_reason_added_later", "UNSAFE_STORE_PERMISSIONS"),
])
def test_create_refusals_other_than_already_exists_fail_closed(tmp_path, acl, reason, code):
    acl.ensure_reason = reason
    with pytest.raises(DropError, match=f"^{code}$"):
        store.secure_directory(tmp_path / "vault")
    assert not any(call[0] in ("verify", "make") for call in acl.calls)


def test_mode_bits_never_count_as_a_pass(tmp_path, acl):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    target = folder / "drop.sqlite"
    target.write_bytes(b"")
    os.chmod(target, 0o600)
    acl.verify_reason = "foreign_ace"
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        store.secure_directory(folder)
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        store.secure_file(target)


def test_acl_reader_error_fails_closed(tmp_path, acl, monkeypatch):
    def broken(path):
        raise OSError("GetSecurityInfo failed")

    (tmp_path / "existing").mkdir()
    monkeypatch.setattr(owner_only, "verify_owner_only", broken)
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        store.secure_directory(tmp_path / "existing")


def test_make_owner_only_refusal_on_a_new_store_file_fails_closed(tmp_path, acl):
    acl.make_reason = "foreign_ace"
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        EncryptedStore(tmp_path / "vault", secrets.token_bytes(32))
    assert ("make", "drop.sqlite") in acl.calls


def test_make_owner_only_refusal_on_a_fresh_sidecar_fails_closed(tmp_path, acl):
    acl.make_by_name["drop.sqlite-wal"] = "dacl_not_protected"
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        EncryptedStore(tmp_path / "vault", secrets.token_bytes(32))
    assert ("make", "drop.sqlite-wal") in acl.calls


def test_existing_sidecar_is_verified_before_sqlite_and_never_repaired(tmp_path, acl):
    key = secrets.token_bytes(32)
    EncryptedStore(tmp_path / "vault", key).close()
    (tmp_path / "vault" / "drop.sqlite-wal").write_bytes(b"")
    acl.verify_by_name["drop.sqlite-wal"] = "foreign_ace"
    acl.calls.clear()
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PERMISSIONS$"):
        EncryptedStore(tmp_path / "vault", key)
    assert ("verify", "drop.sqlite-wal") in acl.calls
    assert ("make", "drop.sqlite-wal") not in acl.calls


def test_junction_at_store_folder_is_refused_even_when_the_acl_reader_passes_it(tmp_path, acl, monkeypatch):
    (tmp_path / "vault").mkdir()
    _report_reparse_point(monkeypatch, tmp_path / "vault")
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PATH$"):
        store.secure_directory(tmp_path / "vault")
    assert ("verify", "vault") not in acl.calls


def test_reparse_point_file_is_refused_before_any_repair(tmp_path, acl, monkeypatch):
    target = tmp_path / "drop.sqlite-wal"
    target.write_bytes(b"")
    _report_reparse_point(monkeypatch, target)
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PATH$"):
        store.secure_file(target, repair=True)
    assert not any(call[0] == "make" for call in acl.calls)


def test_hard_linked_sidecar_is_refused_before_any_repair(tmp_path, acl):
    original = tmp_path / "elsewhere"
    original.write_bytes(b"")
    sidecar = tmp_path / "drop.sqlite-wal"
    os.link(original, sidecar)
    with pytest.raises(DropError, match=r"^UNSAFE_STORE_PATH$"):
        store.secure_file(sidecar, repair=True)
    assert not any(call[0] == "make" for call in acl.calls)


def test_missing_parent_is_never_created(tmp_path, acl):
    with pytest.raises(FileNotFoundError):
        store.secure_directory(tmp_path / "absent" / "vault")
    assert not (tmp_path / "absent").exists()
    assert not any(call[0] == "ensure" for call in acl.calls)
