"""Rollback content stays exact; access control is established before any content is overwritten."""
from __future__ import annotations

import ctypes as ct
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.execution import artifacts
from core.execution import windows_private_ledger as native
from core.runtime_paths import active_vool_home, configure_runtime_home


@pytest.fixture
def ledger_home(tmp_path):
    previous = active_vool_home()
    configure_runtime_home(tmp_path / "home")
    try:
        yield
    finally:
        configure_runtime_home(previous)


def _assert_private(path):
    if os.name == "nt":
        from tests.windows_acl_assertions import assert_owner_only

        assert_owner_only(path)
        assert_owner_only(path.parent, directory=True)
    else:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("before", ["SYNTHETIC_SECRET\n", "\u96e8\n\u2603\n", ""])
def test_owner_only_ledger_preserves_exact_rollback_content(ledger_home, before):
    records = [{"path": "synthetic.env", "before_text": before, "after_text": "new\n"}]
    artifacts._store_mutation_records("owner-only", records)
    path = artifacts._mutation_log_path("owner-only")
    _assert_private(path)
    assert artifacts._load_mutation_records("owner-only") == records
    assert json.loads(path.read_text(encoding="utf-8")) == records


def test_a_loose_existing_ledger_and_directory_are_tightened_before_update(ledger_home):
    artifacts._store_mutation_records("existing", [{"before_text": "keep exactly\n"}])
    path = artifacts._mutation_log_path("existing")
    if os.name == "nt":
        from tests.windows_acl_assertions import grant_everyone_read

        grant_everyone_read(path.parent)
        grant_everyone_read(path)
    else:
        path.parent.chmod(0o777)
        path.chmod(0o666)
    with pytest.raises(AssertionError):
        _assert_private(path)
    updated = [{"before_text": "keep exactly\n", "after_text": "updated"}]
    artifacts._store_mutation_records("existing", updated)
    _assert_private(path)
    assert artifacts._load_mutation_records("existing") == updated


@pytest.mark.parametrize("failure", ["user", "directory_open", "directory_acl", "file_open", "file_acl"])
def test_windows_acl_admission_failure_never_overwrites_existing_bytes(tmp_path, monkeypatch, failure):
    path = tmp_path / "ledger.json"
    original = b"original rollback bytes\n"
    path.write_bytes(original)
    events = []

    def check(stage):
        events.append(stage)
        if stage == failure:
            raise PermissionError("synthetic ACL failure")

    @contextmanager
    def user():
        check("user")
        yield 1, "synthetic-owner"

    @contextmanager
    def descriptor(owner, directory):
        yield 1, 2

    @contextmanager
    def handle(target, descriptor, directory):
        check("directory_open" if directory else "file_open")
        yield 3 if directory else 4

    def restrict(handle, sid, dacl, directory):
        check("directory_acl" if directory else "file_acl")

    def write(handle, payload):
        pytest.fail("No payload may reach the writer after failed ACL admission")

    monkeypatch.setattr(native, "_Win32", lambda: SimpleNamespace(
        user=user, descriptor=descriptor, handle=handle, restrict=restrict, write=write))
    with pytest.raises(PermissionError, match="synthetic ACL failure"):
        native.write_owner_only_ledger(path, "must not be written")
    assert path.read_bytes() == original
    assert events[-1] == failure


@pytest.mark.parametrize("directory,attributes", [(False, 0x400), (True, 0x410), (False, 0x10), (True, 0x80)])
def test_native_handle_refuses_reparse_points_and_wrong_object_types(tmp_path, directory, attributes):
    closed = []

    def information(handle, kind, buffer, size):
        buffer[0] = attributes
        return True

    api = object.__new__(native._Win32)
    api.kernel = SimpleNamespace(CreateFileW=lambda *args: 123,
                                 GetFileInformationByHandleEx=information,
                                 CloseHandle=lambda handle: closed.append(handle))
    with pytest.raises(PermissionError, match="reparse points or unexpected object types"):
        with api.handle(tmp_path / "object", ct.c_void_p(), directory):
            pytest.fail("An untrusted object must not reach the payload writer")
    assert closed == [123]
