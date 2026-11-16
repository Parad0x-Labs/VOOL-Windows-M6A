"""Windows: the GitHub signer writes only in a disposable workspace and the authority's own folder (D19).

Owner decision D19 A (16 Nov 2026), to Security Review's design bar: the checkout sits inside a folder
the owner marked disposable (VOOL_WINDOWS_DISPOSABLE_WORKSPACES), setup (which runs gh) is refused, and a
run's scratch goes in the authority's folder (%LOCALAPPDATA%\\VOOL-Registry-Authority), never in %TEMP%.
A share, device or NT spelling refuses before anything resolves it. The Windows branch is driven on any OS
through github_authority._WINDOWS and the shell's known folder.
"""
from __future__ import annotations

import tempfile

import pytest

from core import owner_only, windows_known_folders
from core.registry.codec import RegistryError
from registry_service import github_authority, windows_keeper


@pytest.fixture
def windows(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    local.mkdir()
    marked = tmp_path / "marked"
    marked.mkdir()
    monkeypatch.setattr(github_authority, "_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_shell_known_folder", lambda name: str(local) if name == "LocalAppData" else None)
    monkeypatch.setattr(windows_known_folders, "_full_local_path", lambda value: True)
    monkeypatch.setenv("VOOL_WINDOWS_DISPOSABLE_WORKSPACES", str(marked))
    return marked


def _authority_folder():
    folder = windows_keeper.authority_folder()
    owner_only.ensure_private_dir(folder, fresh=True)
    return folder


def test_a_checkout_outside_a_marked_folder_is_refused(windows, tmp_path) -> None:
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)

    with pytest.raises(RegistryError, match="disposable workspace"):
        github_authority.Repository(outside)
    github_authority.Repository(windows / "checkout")


@pytest.mark.parametrize("spelled", [r"\\server\share\signer", "//server/share/signer", r"\\?\C:\signer",
                                     r"\\.\pipe\signer"])
def test_a_share_or_device_spelling_is_refused(windows, spelled) -> None:
    with pytest.raises(RegistryError, match=r"disposable workspace|not a folder VOOL writes"):
        github_authority.Repository(spelled)


def test_setup_is_refused_on_windows_before_anything_is_sent_or_made(windows, tmp_path) -> None:
    """Setup hands secrets to gh, a program VOOL doesn't run on Windows: set up once on a Mac or Linux."""
    sent = {}
    repo = windows / "repo"
    (repo / ".git").mkdir(parents=True)

    with pytest.raises(RegistryError, match="set up on a Mac or Linux once"):
        github_authority.setup(repo, offline=windows / "usb", github="o/r", catalog_key="ab" * 32, targets=[],
                               passphrase="correct horse battery", review={"provider": "openrouter", "model": "m"},
                               vool_commit="c" * 40, now=1, set_secret=sent.__setitem__)
    assert sent == {} and not (windows / "usb").exists() and not (repo / github_authority.FOLDER).exists()


def test_a_runs_scratch_is_in_the_authority_folder_not_temp(windows, tmp_path, monkeypatch) -> None:
    folder = _authority_folder()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "TEMP"))

    scratch = github_authority._scratch()

    assert scratch.parent == folder / "scratch" and scratch.is_dir()
    assert owner_only.verify_owner_only(scratch).ok and owner_only.verify_owner_only(folder / "scratch").ok
    assert not (tmp_path / "TEMP").exists()


def test_without_the_authority_folder_a_run_refuses_and_makes_nothing(windows) -> None:
    with pytest.raises(RegistryError, match="authority folder is missing"):
        github_authority._scratch()
    assert not windows_keeper.authority_folder().exists()


def test_off_windows_the_scratch_stays_a_private_temporary_folder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(github_authority, "_WINDOWS", False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    scratch = github_authority._scratch()

    assert scratch.parent == tmp_path and scratch.name.startswith("skill-codex-")
