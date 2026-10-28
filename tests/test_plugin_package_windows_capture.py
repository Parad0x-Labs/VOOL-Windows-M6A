"""Plugin package capture works on Windows, still refuses links, and its execution copy can be removed.

`tests/_windows_capture_rules.py` makes a POSIX run take the Windows path (no `O_NOFOLLOW`, no `dir_fd`,
read-only files cannot be deleted); on Windows the real API runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.plugin_package import PackageError, capture_package, execution_directory
from tests import _windows_capture_rules


def _package(root: Path) -> Path:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "pack", "version": "1.0.0"}))
    (root / "LICENSE").write_text("MIT\n")
    (root / "bin").mkdir()
    (root / "bin" / "run").write_text("print('hello')\n")
    (root / "vendor" / "empty").mkdir(parents=True)
    return root


def _link_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this host cannot make links: {exc}")


def test_the_windows_path_captures_the_same_identity(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    posix = None if _windows_capture_rules.WINDOWS else capture_package(pack)
    _windows_capture_rules.apply(monkeypatch)
    identity = capture_package(pack)
    assert identity.manifest == {"name": "pack", "version": "1.0.0"}
    assert [entry["path"] for entry in identity.entries] == [
        ".codex-plugin", ".codex-plugin/plugin.json", "LICENSE", "bin", "bin/run", "vendor", "vendor/empty"]
    if posix is not None:
        assert identity.digest == posix.digest


def test_windows_never_reads_an_executable_bit_from_the_file_mode(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    (pack / "bin" / "run.cmd").write_text("@echo off\n")
    (pack / "bin" / "run").chmod(0o755)
    _windows_capture_rules.apply(monkeypatch)
    entries = {entry["path"]: entry for entry in capture_package(pack).entries}
    assert entries["bin/run"]["executable"] == 0
    assert entries["bin/run.cmd"]["executable"] == 0


def test_the_execution_copy_is_captured_and_removed(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    _windows_capture_rules.apply(monkeypatch)
    with execution_directory(tmp_path / "scratch") as owned:
        copied = capture_package(pack, destination=owned / "package")
        assert (owned / "package" / "bin" / "run").read_text() == "print('hello')\n"
        assert capture_package(owned / "package").digest == copied.digest
    assert not owned.exists()


def test_a_linked_file_is_refused(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    _link_or_skip(pack / "bin" / "secret", outside)
    _windows_capture_rules.apply(monkeypatch)
    with pytest.raises(PackageError):
        capture_package(pack)


def test_a_linked_directory_is_refused(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("data\n")
    _link_or_skip(pack / "linked", outside, directory=True)
    _windows_capture_rules.apply(monkeypatch)
    with pytest.raises(PackageError):
        capture_package(pack)


def test_a_linked_package_root_is_refused(tmp_path, monkeypatch):
    pack = _package(tmp_path / "pack")
    _link_or_skip(tmp_path / "alias", pack, directory=True)
    _windows_capture_rules.apply(monkeypatch)
    with pytest.raises(PackageError):
        capture_package(tmp_path / "alias")
