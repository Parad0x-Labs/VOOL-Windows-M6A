"""Installed bundle state stays per-user, including after an older internal install."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from installer.bundle.migrate_install_state import (
    MigrationError,
    migrate_legacy_home,
    remove_generated_bytecode,
)


def test_legacy_user_state_moves_without_overwriting_existing_home(tmp_path: Path) -> None:
    root = tmp_path / "program" / "VOOL"
    legacy = root / "app" / ".vool_local"
    (legacy / "config").mkdir(parents=True)
    (legacy / "data" / "preflight").mkdir(parents=True)
    (legacy / "config" / "user.json").write_bytes(b"legacy settings")
    (legacy / "data" / "preflight" / "report.json").write_bytes(b"legacy report")
    home = tmp_path / "profile" / "VOOL"
    (home / "data").mkdir(parents=True)
    (home / "data" / "memory.db").write_bytes(b"current memory")

    assert migrate_legacy_home(root, home) == 2
    assert (home / "config" / "user.json").read_bytes() == b"legacy settings"
    assert (home / "data" / "preflight" / "report.json").read_bytes() == b"legacy report"
    assert (home / "data" / "memory.db").read_bytes() == b"current memory"
    assert not legacy.exists()
    assert migrate_legacy_home(root, home) == 0


def test_conflicting_user_state_fails_without_deleting_legacy(tmp_path: Path) -> None:
    root = tmp_path / "program" / "VOOL"
    legacy = root / "app" / ".vool_local"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "memory.db").write_bytes(b"old memory")
    home = tmp_path / "profile" / "VOOL"
    (home / "data").mkdir(parents=True)
    (home / "data" / "memory.db").write_bytes(b"new memory")

    with pytest.raises(MigrationError, match="conflict"):
        migrate_legacy_home(root, home)
    assert (legacy / "data" / "memory.db").read_bytes() == b"old memory"
    assert (home / "data" / "memory.db").read_bytes() == b"new memory"


def test_redirected_legacy_state_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "program" / "VOOL"
    external = tmp_path / "external"
    external.mkdir()
    (external / "wallet.txt").write_bytes(b"untouched")
    (root / "app").mkdir(parents=True)
    try:
        os.symlink(external, root / "app" / ".vool_local", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(MigrationError, match="regular directory"):
        migrate_legacy_home(root, tmp_path / "profile" / "VOOL")
    assert (external / "wallet.txt").read_bytes() == b"untouched"


def test_only_generated_bytecode_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "program" / "VOOL"
    cache = root / "app" / "core" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "runtime.cpython-312.pyc").write_bytes(b"bytecode")
    source = root / "app" / "core" / "runtime.py"
    source.write_bytes(b"source")

    assert remove_generated_bytecode(root) == 1
    assert not cache.exists()
    assert source.read_bytes() == b"source"


def test_unexpected_cache_file_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "program" / "VOOL"
    cache = root / "app" / "core" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "user-note.txt").write_bytes(b"retain")

    with pytest.raises(MigrationError, match="non-bytecode"):
        remove_generated_bytecode(root)
    assert (cache / "user-note.txt").read_bytes() == b"retain"
