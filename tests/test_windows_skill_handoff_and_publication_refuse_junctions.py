"""The skill hand-off and the public projection refuse a Windows junction like a symlink.

core/registry/handoff.py and registry_service/publication.py came in from Mac checking ``is_symlink``.
On Windows a directory junction (``mklink /J``, no privilege needed) redirects a path just like a
symlink, yet ``is_symlink`` reports False. core/fs_links.py is the port's one link definition (a
name-surrogate reparse tag counts); a junction is faked here by giving ``os.lstat`` that tag.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import fs_links
from core.registry.codec import RegistryError


class _JunctionStat:
    def __init__(self, real: os.stat_result) -> None:
        self._real = real
        self.st_reparse_tag = fs_links.IO_REPARSE_TAG_MOUNT_POINT

    def __getattr__(self, name: str):
        return getattr(self._real, name)


@pytest.fixture
def junctions(monkeypatch):
    marked: set[str] = set()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        return _JunctionStat(result) if os.path.abspath(os.fspath(path)) in marked else result

    monkeypatch.setattr(os, "lstat", lstat)
    return lambda path: marked.add(os.path.abspath(os.fspath(path)))


def _publish(directory: Path) -> None:
    from registry_service import publication

    publication.publish(directory, [], [], {"tested": [], "curation": {}}, artifact=lambda sha: b"",
                        presentation={"marks": {}, "metrics": []})


def test_publication_refuses_a_junction_as_its_directory(tmp_path, junctions) -> None:
    directory = tmp_path / "public"
    directory.mkdir()
    junctions(directory)
    with pytest.raises(RegistryError, match="publication directory"):
        _publish(directory)


def test_publication_refuses_a_junction_as_its_package_directory(tmp_path, junctions) -> None:
    directory = tmp_path / "public"
    (directory / "packages").mkdir(parents=True)
    junctions(directory / "packages")
    with pytest.raises(RegistryError, match="package directory"):
        _publish(directory)


def test_handoff_refuses_a_skill_folder_that_is_a_junction(tmp_path, junctions) -> None:
    from core.registry import handoff
    from core.skill_package import PackageRefused

    skill = tmp_path / "local-skills" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    junctions(skill)
    with pytest.raises(PackageRefused, match="link"):
        handoff._made_here(skill, "global")


def test_handoff_counts_a_junction_inside_the_skill_as_an_extra_file(tmp_path, junctions) -> None:
    from core.registry import handoff

    skill = tmp_path / "my-skill"
    (skill / "linked").mkdir(parents=True)
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    junctions(skill / "linked")
    assert "linked" in handoff._extra_files(skill)
