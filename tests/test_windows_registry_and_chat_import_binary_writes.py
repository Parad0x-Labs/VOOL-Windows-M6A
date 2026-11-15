"""The chat-import upload, the online catalog key and the catalog file are opened with O_BINARY (K5).

On Windows ``os.open`` without ``O_BINARY`` hands back a text-mode descriptor, and ``os.fdopen(fd, "wb")``
keeps that mode: every 0x0A written lands as 0x0D 0x0A. A zip upload, a raw 32-byte Ed25519 key and
the canonical catalog document are bytes, so each must be opened binary. ``os.O_BINARY`` gets the
Windows value here and every ``os.open`` is recorded, so a POSIX run catches the missing flag.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _windows_text_mode_rules as text_mode


@pytest.fixture
def opened(monkeypatch):
    real_open = os.open
    seen: dict[str, int] = {}

    def open_(path, flags, mode=0o777, *, dir_fd=None):
        seen[os.fspath(path)] = flags
        return real_open(path, flags & ~text_mode.O_BINARY, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "O_BINARY", text_mode.O_BINARY, raising=False)
    monkeypatch.setattr(os, "open", open_)
    return seen


def _binary(seen: dict[str, int], path: Path) -> bool:
    return bool(seen[os.fspath(path)] & text_mode.O_BINARY)


def test_chat_import_upload_is_opened_binary(opened, tmp_path, monkeypatch) -> None:
    from core import runtime_paths
    from core.chat_import import flow

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "home"))
    runtime_paths.configure_runtime_home(tmp_path / "home")
    try:
        staged = flow.stage_upload(b"PK\x03\x04\n\r\n\x1a")
        path = flow._UPLOADS[staged["upload_id"]]
        assert _binary(opened, path)
        assert path.read_bytes() == b"PK\x03\x04\n\r\n\x1a"
    finally:
        flow._discard_uploads()
        runtime_paths.configure_runtime_home(None)


def test_online_catalog_key_is_opened_binary(opened, tmp_path) -> None:
    from registry_service import authority

    path = tmp_path / "catalog.key"
    authority.create_online_key(path)
    assert _binary(opened, path)
    assert len(path.read_bytes()) == 32


def test_catalog_file_is_opened_binary(opened, tmp_path) -> None:
    from core.registry.codec import canonical
    from registry_service import authority

    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_bytes(canonical({"schema": "x"}))

    class _Catalog:
        pass

    holder = _Catalog()
    holder.catalog_file = catalog_file
    authority.OnlineCatalog._write(holder, {"signed": {"line": "a\nb"}})
    assert _binary(opened, catalog_file.with_name(catalog_file.name + ".new"))
