"""On Windows a mode-bit check never counts as a pass (K4).

``st_mode`` on Windows only reflects the read-only attribute and ``chmod(0o700)`` only toggles it, so
a folder or key file "checked" by its mode bits says nothing about who can read it. Each place below
takes the owner-only ACL helpers (core/owner_only.py) on Windows instead, or refuses plainly:

- the chat-import snapshot folder (core/chat_import/sqlite_snapshot.py),
- the online catalog key (registry_service/authority.py),
- the service's private folder (registry_service/cli.py),
- the signing Mac's authority (registry_service/mac_authority.py), which must still import on Windows
  (``fcntl`` is POSIX-only) and must refuse to run there.

The Windows branch is driven by each module's ``_IS_WINDOWS`` switch with the ACL helpers faked.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core import owner_only
from core.owner_only import OwnerOnlyReport, OwnerOnlyUnavailable
from core.registry.codec import RegistryError


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (1)")
    return path


# --------------------------------------------------------------------------- chat-import snapshot


def test_snapshot_folder_is_made_owner_only_by_acl_on_windows(tmp_path, monkeypatch) -> None:
    from core.chat_import import sqlite_snapshot

    calls: list[tuple[str, bool]] = []

    def make(path, *, directory=False):
        calls.append((os.fspath(path), directory))
        return OwnerOnlyReport(True)

    monkeypatch.setattr(sqlite_snapshot, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "make_owner_only", make)
    with sqlite_snapshot.snapshot_connection(_store(tmp_path), max_bytes=1 << 20, work_dir=tmp_path) as db:
        assert db.execute("SELECT x FROM t").fetchall() == [(1,)]
    assert len(calls) == 1 and calls[0][1] is True
    assert Path(calls[0][0]).parent == tmp_path


def test_snapshot_refuses_when_the_folder_cannot_be_made_owner_only_on_windows(tmp_path, monkeypatch) -> None:
    from core.chat_import import sqlite_snapshot

    monkeypatch.setattr(sqlite_snapshot, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, *, directory=False: OwnerOnlyReport(False, "set_failed"))
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(sqlite_snapshot.SnapshotError) as refused:
        with sqlite_snapshot.snapshot_connection(_store(tmp_path), max_bytes=1 << 20, work_dir=work):
            pytest.fail("a snapshot was read from a folder that is not private")
    assert refused.value.code == "store_unreadable"
    assert list(work.iterdir()) == []  # nothing copied is left behind


# --------------------------------------------------------------------------- online catalog key


def test_online_key_mode_bits_do_not_pass_on_windows(tmp_path, monkeypatch) -> None:
    from registry_service import authority

    path = tmp_path / "catalog.key"
    path.write_bytes(b"\x01" * 32)
    os.chmod(path, 0o600)
    monkeypatch.setattr(authority, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda p: OwnerOnlyReport(False, "ace_not_owner_only"))
    with pytest.raises(RegistryError, match="readable by others"):
        authority.load_online_key(path)


def test_online_key_loads_on_windows_when_its_acl_is_owner_only(tmp_path, monkeypatch) -> None:
    from registry_service import authority

    path = tmp_path / "catalog.key"
    path.write_bytes(b"\x01" * 32)
    os.chmod(path, 0o644)  # Windows modes say nothing; the ACL decides
    monkeypatch.setattr(authority, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda p: OwnerOnlyReport(True))
    assert authority.load_online_key(path)


def test_online_key_is_made_owner_only_before_its_bytes_are_written_on_windows(tmp_path, monkeypatch) -> None:
    from registry_service import authority

    path = tmp_path / "catalog.key"
    sizes: list[int] = []

    def require(p, *, directory=False):
        sizes.append(Path(p).stat().st_size)

    monkeypatch.setattr(authority, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "require_owner_only", require)
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda p: OwnerOnlyReport(True))
    authority.create_online_key(path)
    assert sizes == [0]
    assert len(path.read_bytes()) == 32


def test_online_key_is_not_written_when_it_cannot_be_made_owner_only_on_windows(tmp_path, monkeypatch) -> None:
    from registry_service import authority

    path = tmp_path / "catalog.key"

    def require(p, *, directory=False):
        raise OwnerOnlyUnavailable(p, "set_failed")

    monkeypatch.setattr(authority, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "require_owner_only", require)
    with pytest.raises(OwnerOnlyUnavailable):
        authority.create_online_key(path)
    assert not path.exists()


# --------------------------------------------------------------------------- service private folder


def _service_config(tmp_path: Path) -> Path:
    config = tmp_path / "service.json"
    config.write_text(json.dumps({"schema": "vool.registry.service.v1", "private_dir": str(tmp_path / "private")}),
                      encoding="utf-8")
    return config


def test_catalog_key_takes_the_owner_only_folder_on_windows(tmp_path, monkeypatch, capsys) -> None:
    from registry_service import cli

    made: list[str] = []

    def ensure(path, *, fresh=False):
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o755)  # Windows modes say nothing; the ACL decides
        made.append(os.fspath(path))
        return Path(path)

    monkeypatch.setattr(cli, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "ensure_private_dir", ensure)
    monkeypatch.setattr(owner_only, "require_owner_only", lambda p, *, directory=False: None)
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda p: OwnerOnlyReport(True))

    class _Args:
        config = _service_config(tmp_path)

    assert cli.catalog_key(_Args()) == 0, capsys.readouterr().out
    assert made == [str((tmp_path / "private").resolve())]


def test_catalog_key_refuses_on_windows_when_the_folder_cannot_be_owner_only(tmp_path, monkeypatch, capsys) -> None:
    from registry_service import cli

    def ensure(path, *, fresh=False):
        raise OwnerOnlyUnavailable(path, "set_failed")

    monkeypatch.setattr(cli, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(owner_only, "ensure_private_dir", ensure)

    class _Args:
        config = _service_config(tmp_path)

    assert cli.catalog_key(_Args()) == 1
    assert "refused" in capsys.readouterr().out
    assert not (tmp_path / "private" / "catalog.key").exists()


# --------------------------------------------------------------------------- signing Mac authority


def test_mac_authority_imports_without_fcntl() -> None:
    """github_authority and publication import from mac_authority, so a missing fcntl must not break them."""
    code = ("import sys; sys.modules['fcntl'] = None\n"
            "import registry_service.mac_authority, registry_service.publication, registry_service.github_authority\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                            cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr


def test_mac_authority_refuses_plainly_on_windows(tmp_path, monkeypatch) -> None:
    from registry_service import mac_authority

    monkeypatch.setattr(mac_authority, "_IS_WINDOWS", True, raising=False)
    folder = tmp_path / "authority"
    folder.mkdir(mode=0o700)
    with pytest.raises(RegistryError, match="Mac"):
        mac_authority.Authority(folder)
    with pytest.raises(RegistryError, match="Mac"):
        mac_authority.setup(tmp_path / "new", server="https://example.invalid", catalog_key="0" * 64, targets=[],
                            review={"provider": "ollama", "model": "m"}, passphrase="x" * 12, now=0)
    assert not (tmp_path / "new").exists()
