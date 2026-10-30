"""Owner-only files and folders (B3, jobs/broker/B3-owner-only-acl/DESIGN.md).

On Windows ``chmod(0o600)`` only toggles the read-only bit, so a "private" file keeps the parent
folder's inherited ACL; under a non-profile root (``C:\\VOOL``, a second drive) that often lets
``BUILTIN\\Users`` read it. ``core.owner_only`` sets a protected, owner-only DACL by handle and
reads it back. The policy half (what counts as owner-only, what fails closed) runs everywhere
against a fake Windows API; the real-API half runs on Windows only.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyUnavailable

_USER = "S-1-5-21-1-2-3-1001"
_OTHER = "S-1-5-32-545"  # BUILTIN\Users
_ALLOWED = 0
_DENIED = 1


class _FakeWinApi:
    """The Windows calls ``core.owner_only`` makes, as plain data: each path holds a descriptor
    ``(owner, protected, dacl)``, where ``dacl`` is a list of ``(ace_type, sid)`` or None."""

    def __init__(self) -> None:
        self.descriptors: dict[str, tuple[str, bool, list[tuple[int, str]] | None]] = {}
        self.reparse: set[str] = set()
        self.directories: set[str] = set()
        self.no_acls: set[str] = set()
        self.set_fails: set[str] = set()
        self.set_is_ignored: set[str] = set()
        self.set_calls: list[tuple[str, str]] = []
        self.open_handles = 0

    def user_sid(self) -> str:
        return _USER

    def open(self, path: str) -> str:
        if path not in self.descriptors:
            raise FileNotFoundError(path)
        self.open_handles += 1
        return path

    def close(self, handle: str) -> None:
        self.open_handles -= 1

    def is_reparse_point(self, handle: str) -> bool:
        return handle in self.reparse

    def is_directory(self, handle: str) -> bool:
        return handle in self.directories

    def has_persistent_acls(self, handle: str) -> bool:
        return handle not in self.no_acls

    def set_descriptor(self, handle: str, sddl: str) -> None:
        self.set_calls.append((handle, sddl))
        if handle in self.set_fails:
            raise OSError(5, "Access is denied")
        if handle in self.set_is_ignored:
            return
        self.descriptors[handle] = (_USER, True, [(_ALLOWED, _USER)])

    def read_descriptor(self, handle: str) -> tuple[str, bool, list[tuple[int, str]] | None]:
        return self.descriptors[handle]

    def create_directory(self, path: str, sddl: str) -> bool:
        if path in self.descriptors:
            return False
        self.descriptors[path] = (_USER, True, [(_ALLOWED, _USER)])
        self.directories.add(path)
        return True


@pytest.fixture
def fake(monkeypatch):
    api = _FakeWinApi()
    monkeypatch.setattr(owner_only, "_WINDOWS", True)
    monkeypatch.setattr(owner_only, "_win_api", lambda: api)
    return api


def _path(name: str) -> str:
    return os.path.abspath(os.path.join("vool-owner-only-fake", name))


# --- verify: only a protected DACL of allow-ACEs for the user passes ---------------------------------
@pytest.mark.parametrize(
    ("descriptor", "reason"),
    [
        ((_USER, True, [(_ALLOWED, _USER)]), ""),
        ((_USER, True, None), "null_dacl"),
        ((_USER, False, [(_ALLOWED, _USER)]), "dacl_not_protected"),
        ((_OTHER, True, [(_ALLOWED, _USER)]), "owner_not_user"),
        ((_USER, True, [(_ALLOWED, _USER), (_ALLOWED, _OTHER)]), "foreign_ace"),
        ((_USER, True, [(_DENIED, _USER)]), "foreign_ace"),
        ((_USER, True, []), "empty_dacl"),
    ],
)
def test_verify_accepts_only_a_protected_user_only_dacl(fake, descriptor, reason):
    path = _path("f.key")
    fake.descriptors[path] = descriptor
    report = owner_only.verify_owner_only(path)
    assert report.ok is (reason == "")
    assert report.reason == reason
    assert fake.open_handles == 0


def test_verify_refuses_a_reparse_point_without_reading_through_it(fake):
    path = _path("link.key")
    fake.descriptors[path] = (_USER, True, [(_ALLOWED, _USER)])
    fake.reparse.add(path)
    assert owner_only.verify_owner_only(path).reason == "reparse_point"


def test_verify_reports_a_missing_file_as_not_owner_only(fake):
    report = owner_only.verify_owner_only(_path("absent.key"))
    assert not report.ok
    assert report.reason == "unreadable"


# --- make: set by handle, then read back -------------------------------------------------------------
def test_make_sets_a_protected_owner_only_descriptor_and_verifies_it(fake):
    path = _path("f.key")
    fake.descriptors[path] = (_OTHER, False, [(_ALLOWED, _USER), (_ALLOWED, _OTHER)])
    report = owner_only.make_owner_only(path)
    assert report.ok, report
    assert fake.set_calls == [(path, f"O:{_USER}D:P(A;;FA;;;{_USER})")]
    assert fake.open_handles == 0


def test_a_directory_gets_an_inheritable_ace_so_files_inside_are_born_private(fake):
    path = _path("keys")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.directories.add(path)
    assert owner_only.make_owner_only(path, directory=True).ok
    assert fake.set_calls == [(path, f"O:{_USER}D:P(A;OICI;FA;;;{_USER})")]


def test_make_refuses_when_the_kind_is_not_the_one_asked_for(fake):
    path = _path("keys")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.directories.add(path)
    assert owner_only.make_owner_only(path).reason == "kind_mismatch"
    assert fake.set_calls == []


def test_make_refuses_a_reparse_point_and_never_sets_its_target(fake):
    path = _path("link.key")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.reparse.add(path)
    assert owner_only.make_owner_only(path).reason == "reparse_point"
    assert fake.set_calls == []


def test_make_refuses_a_volume_without_persistent_acls(fake):
    path = _path("f.key")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.no_acls.add(path)
    assert owner_only.make_owner_only(path).reason == "no_persistent_acls"
    assert fake.set_calls == []


def test_a_failed_set_is_reported_not_raised(fake):
    path = _path("f.key")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.set_fails.add(path)
    report = owner_only.make_owner_only(path)
    assert report.reason == "set_failed"
    assert fake.open_handles == 0


def test_a_set_that_reports_success_but_did_not_stick_fails_the_read_back(fake):
    path = _path("f.key")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.set_is_ignored.add(path)
    assert owner_only.make_owner_only(path).reason == "dacl_not_protected"


# --- tier 1 entry points fail closed -----------------------------------------------------------------
def test_require_raises_when_owner_only_cannot_be_reached(fake):
    path = _path("f.key")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.no_acls.add(path)
    with pytest.raises(OwnerOnlyUnavailable) as raised:
        owner_only.require_owner_only(path)
    assert raised.value.reason == "no_persistent_acls"


def test_ensure_private_dir_creates_the_folder_owner_only(fake, monkeypatch):
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    path = _path("keys")
    assert owner_only.ensure_private_dir(path) == owner_only.Path(path)
    assert path in fake.directories
    assert fake.set_calls == []


def test_ensure_private_dir_repairs_an_existing_folder(fake, monkeypatch):
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    path = _path("keys")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.directories.add(path)
    owner_only.ensure_private_dir(path)
    assert fake.set_calls == [(path, f"O:{_USER}D:P(A;OICI;FA;;;{_USER})")]


def test_ensure_private_dir_raises_when_the_folder_cannot_be_made_private(fake, monkeypatch):
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    path = _path("keys")
    fake.descriptors[path] = (_USER, False, [(_ALLOWED, _OTHER)])
    fake.directories.add(path)
    fake.set_fails.add(path)
    with pytest.raises(OwnerOnlyUnavailable):
        owner_only.ensure_private_dir(path)


def test_a_long_path_is_handed_to_windows_with_the_long_path_prefix():
    long_path = "C:\\" + "\\".join(["d" * 50] * 6) + "\\f.key"
    assert owner_only._win_long_path(long_path).startswith("\\\\?\\C:\\")
    assert owner_only._win_long_path("\\\\srv\\share\\" + "d" * 260).startswith("\\\\?\\UNC\\srv\\share\\")
    assert owner_only._win_long_path("C:\\short\\f.key") == "C:\\short\\f.key"


# --- POSIX ---------------------------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_posix_make_owner_only_sets_mode_and_verifies(tmp_path):
    target = tmp_path / "f.key"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o644)
    assert owner_only.make_owner_only(target).ok
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    folder = tmp_path / "keys"
    folder.mkdir(mode=0o755)
    assert owner_only.make_owner_only(folder, directory=True).ok
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_posix_verify_refuses_group_or_world_access_and_symlinks(tmp_path):
    target = tmp_path / "f.key"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o640)
    assert owner_only.verify_owner_only(target).reason == "mode_too_open"
    link = tmp_path / "link.key"
    link.symlink_to(target)
    assert owner_only.make_owner_only(link).reason == "reparse_point"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


# --- real Windows API ------------------------------------------------------------------------------
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows security descriptors")


@windows_only
def test_windows_file_becomes_owner_only_and_reads_back(tmp_path):
    target = tmp_path / "f.key"
    target.write_text("secret", encoding="utf-8")
    assert not owner_only.verify_owner_only(target).ok  # inherited, unprotected
    report = owner_only.make_owner_only(target)
    assert report.ok, report
    assert owner_only.verify_owner_only(target).ok
    assert target.read_text(encoding="utf-8") == "secret"


@windows_only
def test_windows_private_dir_hands_its_owner_only_ace_to_new_files(tmp_path):
    folder = owner_only.ensure_private_dir(tmp_path / "keys")
    assert owner_only.verify_owner_only(folder).ok
    child = folder / "f.key"
    child.write_text("secret", encoding="utf-8")
    api = owner_only._win_api()
    handle = api.open(str(child))
    try:
        _owner, _protected, dacl = api.read_descriptor(handle)
    finally:
        api.close(handle)
    assert dacl, dacl
    assert {sid for _type, sid in dacl} == {api.user_sid()}
    assert owner_only.make_owner_only(child).ok


@windows_only
def test_windows_refuses_a_symlink(tmp_path):
    target = tmp_path / "f.key"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.key"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs Developer Mode or the symlink privilege")
    assert owner_only.make_owner_only(link).reason == "reparse_point"
    assert not owner_only.verify_owner_only(target).ok
