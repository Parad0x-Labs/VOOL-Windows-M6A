"""A Windows directory junction must not carry a reviewed write outside its directory.

Reproduced on Windows 11 (2026-10-18): `pinned_atomic_write_text(<ws>/linked/escaped.txt)`, where
`linked` is a junction to a directory outside the workspace, was allowed and the file landed
outside. `S_ISLNK`/`islink`/`is_symlink` all report False for junctions, and creating one needs
no privilege. `core.fs_links` is the one link definition; this file pins it on every platform
and drives a real junction where the platform has them.
"""
from __future__ import annotations

import os
import stat
import types
from pathlib import Path

import pytest

from core import fs_links
from core.execution.artifacts import DestinationChangedError, pinned_atomic_write_text

IO_REPARSE_TAG_CLOUD_6 = 0x9000601A  # a OneDrive / cloud-files placeholder: not a link


def _entry(mode: int, tag: int = 0) -> types.SimpleNamespace:
    return types.SimpleNamespace(st_mode=mode, st_reparse_tag=tag)


def test_the_link_definition_covers_symlinks_and_junctions_but_not_cloud_placeholders() -> None:
    assert fs_links.is_link_stat(_entry(stat.S_IFLNK | 0o777))
    assert fs_links.is_link_stat(_entry(stat.S_IFDIR | 0o755, fs_links.IO_REPARSE_TAG_MOUNT_POINT))
    assert fs_links.is_link_stat(_entry(stat.S_IFREG | 0o644, fs_links.IO_REPARSE_TAG_SYMLINK))
    assert fs_links.is_link_stat(_entry(stat.S_IFREG | 0o644, 0xA000001D))   # IO_REPARSE_TAG_LX_SYMLINK
    assert fs_links.is_link_stat(_entry(stat.S_IFDIR | 0o755, 0xA0000027))   # IO_REPARSE_TAG_WCI_LINK
    assert not fs_links.is_link_stat(_entry(stat.S_IFDIR | 0o755, IO_REPARSE_TAG_CLOUD_6))
    assert not fs_links.is_link_stat(_entry(stat.S_IFREG | 0o644, 0x80000013))  # IO_REPARSE_TAG_DEDUP
    assert not fs_links.is_link_stat(_entry(stat.S_IFREG | 0o644))
    assert not fs_links.is_link_stat(types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644))  # POSIX stat


def test_a_missing_path_is_not_a_link(tmp_path: Path) -> None:
    assert fs_links.is_link(tmp_path / "absent") is False


windows_only = pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows filesystem feature")


def _junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@windows_only
def test_a_reviewed_write_through_a_junction_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _junction(workspace / "linked", outside)
    assert fs_links.is_link(workspace / "linked")

    with pytest.raises(DestinationChangedError):
        pinned_atomic_write_text(workspace / "linked" / "escaped.txt", "payload", expected_prior_sha256="")

    assert list(outside.iterdir()) == []


@windows_only
def test_a_junction_as_the_target_name_itself_is_refused(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _junction(workspace / "notes", outside)

    with pytest.raises(DestinationChangedError):
        pinned_atomic_write_text(workspace / "notes", "payload")

    assert list(outside.iterdir()) == []
    assert fs_links.is_link(workspace / "notes"), "the junction itself is left in place, not replaced"
