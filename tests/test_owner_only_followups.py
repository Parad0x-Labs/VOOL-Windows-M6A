"""B3 owner-only follow-ups F1-F3 (jobs/broker/B3-owner-only-acl/review-fixes/NOTES.md).

F1: SetSecurityInfo does not reach what is already inside a folder, so making an existing folder
owner-only left its files on their old inherited ACL. make_tree_owner_only secures the folder,
then each file and folder already inside it, and fails closed on the first one it cannot secure
(a link inside included).

F2: a handle opened before a repair keeps its access. ensure_private_dir(fresh=True) refuses a
folder that already exists, so a secret store can insist on a folder nobody else could have opened.

F3: a mapped network drive letter (Z: on an SMB share) looked local to the string check. A drive
whose type GetDriveTypeW reports as remote, unknown or without a root is now refused as non-local.

The Windows half runs against the fake from tests/test_owner_only.py, reused unchanged.
"""
from __future__ import annotations

import os

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyUnavailable
from tests.test_owner_only import _ALLOWED, _OTHER, _USER, _FakeWinApi


@pytest.fixture
def fake(monkeypatch):
    api = _FakeWinApi()
    monkeypatch.setattr(owner_only, "_WINDOWS", True)
    monkeypatch.setattr(owner_only, "_win_api", lambda: api)
    return api


def _old_acl(api: _FakeWinApi, path, *, directory: bool = False) -> str:
    key = os.path.abspath(os.fspath(path))
    api.descriptors[key] = (_USER, False, [(_ALLOWED, _USER), (_ALLOWED, _OTHER)])
    if directory:
        api.directories.add(key)
    return key


# --- F1 ------------------------------------------------------------------------------------------------
def test_securing_a_tree_reaches_every_file_and_folder_already_inside(fake, tmp_path):
    root = tmp_path / "keys"
    (root / "sub").mkdir(parents=True)
    (root / "a.key").write_bytes(b"a")
    (root / "sub" / "b.key").write_bytes(b"b")
    paths = [_old_acl(fake, root, directory=True), _old_acl(fake, root / "a.key"),
             _old_acl(fake, root / "sub", directory=True), _old_acl(fake, root / "sub" / "b.key")]

    report = owner_only.make_tree_owner_only(root)

    assert report.ok, report.reason
    assert sorted(handle for handle, _ in fake.set_calls) == sorted(paths)
    assert all(owner_only.verify_owner_only(path).ok for path in paths)


def test_a_link_inside_the_tree_fails_it_and_is_never_secured_through(fake, tmp_path):
    root = tmp_path / "keys"
    root.mkdir()
    (root / "link").write_bytes(b"stands in for a reparse point")
    _old_acl(fake, root, directory=True)
    link = _old_acl(fake, root / "link")
    fake.reparse.add(link)

    report = owner_only.make_tree_owner_only(root)

    assert not report.ok and report.reason == "reparse_point"
    assert link not in [handle for handle, _ in fake.set_calls]


def test_a_tree_whose_top_is_a_link_is_not_walked(fake, tmp_path):
    root = tmp_path / "keys"
    root.mkdir()
    (root / "a.key").write_bytes(b"a")
    top = _old_acl(fake, root, directory=True)
    inner = _old_acl(fake, root / "a.key")
    fake.reparse.add(top)

    report = owner_only.make_tree_owner_only(root)

    assert not report.ok and report.reason == "reparse_point"
    assert fake.set_calls == [] and inner in fake.descriptors


def test_a_child_that_cannot_be_secured_fails_the_tree(fake, tmp_path):
    root = tmp_path / "keys"
    root.mkdir()
    (root / "a.key").write_bytes(b"a")
    _old_acl(fake, root, directory=True)
    fake.set_fails.add(_old_acl(fake, root / "a.key"))

    report = owner_only.make_tree_owner_only(root)

    assert not report.ok and report.reason == "set_failed"


def test_a_tree_on_a_share_is_refused(fake):
    report = owner_only.make_tree_owner_only("\\\\srv\\share\\keys")

    assert not report.ok and report.reason == "non_local_path"
    assert fake.open_handles == 0 and fake.set_calls == []


@pytest.mark.skipif(os.name == "nt", reason="the POSIX half")
def test_posix_tree_is_owner_only_to_the_last_file(tmp_path):
    root = tmp_path / "keys"
    (root / "sub").mkdir(parents=True)
    for path in (root / "a.key", root / "sub" / "b.key"):
        path.write_bytes(b"x")
        path.chmod(0o644)
    root.chmod(0o755)
    (root / "sub").chmod(0o755)

    assert owner_only.make_tree_owner_only(root).ok
    for path in (root, root / "sub", root / "a.key", root / "sub" / "b.key"):
        assert owner_only.verify_owner_only(path).ok, path


# --- F2 ------------------------------------------------------------------------------------------------
def test_a_fresh_private_dir_refuses_a_folder_that_already_exists(fake, monkeypatch):
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    path = os.path.abspath("vool-owner-only-fake/keys")
    fake.descriptors[path] = (_USER, True, [(_ALLOWED, _USER, 0x3)])
    fake.directories.add(path)

    with pytest.raises(OwnerOnlyUnavailable) as raised:
        owner_only.ensure_private_dir(path, fresh=True)

    assert raised.value.reason == "already_exists"
    assert fake.set_calls == []


def test_a_fresh_private_dir_is_created_owner_only(fake, monkeypatch):
    monkeypatch.setattr(owner_only.os, "makedirs", lambda *_a, **_k: None)
    path = os.path.abspath("vool-owner-only-fake/new-keys")

    assert owner_only.ensure_private_dir(path, fresh=True) == owner_only.Path(path)
    assert path in fake.directories and fake.set_calls == []


@pytest.mark.skipif(os.name == "nt", reason="the POSIX half")
def test_posix_fresh_private_dir_refuses_an_existing_folder(tmp_path):
    existing = tmp_path / "keys"
    existing.mkdir(mode=0o700)

    with pytest.raises(OwnerOnlyUnavailable) as raised:
        owner_only.ensure_private_dir(existing, fresh=True)
    assert raised.value.reason == "already_exists"
    assert owner_only.ensure_private_dir(tmp_path / "new", fresh=True).is_dir()


# --- F3 ------------------------------------------------------------------------------------------------
_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR, _DRIVE_REMOVABLE, _DRIVE_FIXED, _DRIVE_REMOTE = 0, 1, 2, 3, 4


@pytest.mark.parametrize(
    ("drive_type", "non_local"),
    [(_DRIVE_REMOTE, True), (_DRIVE_UNKNOWN, True), (_DRIVE_NO_ROOT_DIR, True), (_DRIVE_FIXED, False), (_DRIVE_REMOVABLE, False)],
)
def test_a_drive_letter_is_judged_by_its_drive_type(monkeypatch, drive_type, non_local):
    asked: list[str] = []
    monkeypatch.setattr(owner_only, "_drive_type", lambda root: asked.append(root) or drive_type)
    monkeypatch.setattr(owner_only.os.path, "splitdrive", lambda p: ("Z:", p[2:]) if p[:2] == "Z:" else ("", p))
    monkeypatch.setattr(owner_only.os.path, "abspath", lambda p: p)

    assert owner_only._non_local("Z:\\VOOL\\keys") is non_local
    assert asked and asked[0].upper() == "Z:\\"


def test_a_mapped_network_drive_is_refused_before_anything_is_opened(fake, monkeypatch):
    monkeypatch.setattr(owner_only, "_drive_type", lambda root: _DRIVE_REMOTE)
    monkeypatch.setattr(owner_only.os.path, "splitdrive", lambda p: ("Z:", p[2:]) if p[:2] == "Z:" else ("", p))
    monkeypatch.setattr(owner_only.os.path, "abspath", lambda p: p)

    assert owner_only.make_owner_only("Z:\\VOOL\\secret.key").reason == "non_local_path"
    assert owner_only.verify_owner_only("Z:\\VOOL\\secret.key").reason == "non_local_path"
    with pytest.raises(OwnerOnlyUnavailable):
        owner_only.ensure_private_dir("Z:\\VOOL\\keys")
    assert fake.open_handles == 0 and fake.set_calls == []


@pytest.mark.skipif(os.name == "nt", reason="the POSIX half")
def test_a_tree_deeper_than_the_recursion_limit_is_still_walked(tmp_path):
    import sys

    root = tmp_path / "keys"
    deepest = root
    for _ in range(120):
        deepest = deepest / "d"
    deepest.mkdir(parents=True)
    (deepest / "a.key").write_bytes(b"a")
    (deepest / "a.key").chmod(0o644)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        report = owner_only.make_tree_owner_only(root)
    finally:
        sys.setrecursionlimit(limit)

    assert report.ok, report.reason
    assert owner_only.verify_owner_only(deepest / "a.key").ok
