"""Windows: VOOL Drop judges its folders and secrets by their ACL and writes only where it may.

0.7.3 ports Drop to Windows. Mode bits and uids say nothing about who can read a file there, so
core/drop/service.py judges every folder and the vault key with core/owner_only. It refuses a junction or
other reparse point, locks through core/os_file_lock, and does not fsync a folder (Windows can't open one).
Outside VOOL's own home it writes only inside a folder the owner marked disposable. The Windows branch is
driven on any OS through service._WINDOWS and store._WINDOWS_ACL. owner_only is the real one here (POSIX
modes on Linux, the ACL on Windows), and the rows that need a refusal replace it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core import owner_only, runtime_paths
from core.drop import DropError, service, store
from core.owner_only import OwnerOnlyReport


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_DROP", "1")
    monkeypatch.setattr(service, "_WINDOWS", True)
    monkeypatch.setattr(store, "_WINDOWS_ACL", True)
    marked = tmp_path / "marked"
    marked.mkdir()
    monkeypatch.setenv("VOOL_WINDOWS_DISPOSABLE_WORKSPACES", str(marked))
    return marked


def test_a_pair_trades_a_packet_under_the_windows_rules(windows, tmp_path) -> None:
    relay = windows / "relay"
    service.LocalRelay(relay, create=True)
    with service.DropHome(windows / "a", relay) as a, service.DropHome(windows / "b", relay) as b:
        sid = b.join(a.invite(b.fingerprint), a.fingerprint)
        assert a.poll() == ["received"]
        selected = tmp_path / "selected.txt"
        selected.write_bytes(os.urandom(193))
        a.send_packet(sid, "The cobalt parcel contains six samples.", selected)
        assert b.poll() == ["received", "received"]
        assert (b.root / sid / "selected.bin").read_bytes() == selected.read_bytes()
        key = b.root / "crypto" / "vault.key"
        assert key.stat().st_size == 32 and owner_only.verify_owner_only(key).ok
        for path in (b.root, b.root / "crypto", b.root / "drops.db", b.root / "actor.lock", relay):
            assert owner_only.verify_owner_only(path).ok, path


@pytest.mark.parametrize("what", ["home", "relay"])
def test_outside_a_marked_folder_nothing_is_made(windows, tmp_path, what) -> None:
    outside = tmp_path / "outside"
    with pytest.raises(DropError, match=r"HOME_NOT_IN_WORKSPACE|RELAY_NOT_IN_WORKSPACE"):
        if what == "home":
            service.DropHome(outside, windows / "relay")
        else:
            service.LocalRelay(outside, create=True)
    assert not outside.exists()


def test_vools_own_home_needs_no_mark(windows, tmp_path, monkeypatch) -> None:
    home = tmp_path / "vool-home"
    runtime_paths.configure_runtime_home(home)
    try:
        relay = windows / "relay"
        service.LocalRelay(relay, create=True)
        with service.DropHome(None, relay) as own:
            assert own.home.resolve() == home.resolve()
    finally:
        runtime_paths.configure_runtime_home(None)


def test_a_linked_relay_is_refused_even_inside_the_marked_folder(windows) -> None:
    elsewhere = windows / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    linked = windows / "linked"
    linked.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(DropError, match="DIRECTORY_PERMISSIONS"):
        service.LocalRelay(linked)


def test_an_existing_folder_that_is_not_owner_only_is_refused_never_repaired(windows, monkeypatch) -> None:
    relay = windows / "relay"
    relay.mkdir()
    made: list[str] = []
    monkeypatch.setattr(owner_only, "verify_owner_only", lambda path: OwnerOnlyReport(False, "foreign_ace"))
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, directory=False: made.append(str(path)))
    with pytest.raises(DropError, match="DIRECTORY_PERMISSIONS"):
        service.LocalRelay(relay)
    assert made == []


def test_a_vault_key_that_is_not_owner_only_is_refused(windows, monkeypatch) -> None:
    crypto = windows / "crypto"
    crypto.mkdir(mode=0o700)
    key = crypto / "vault.key"
    key.write_bytes(b"k" * 32)
    real = owner_only.verify_owner_only
    monkeypatch.setattr(owner_only, "verify_owner_only",
                        lambda path: OwnerOnlyReport(False, "foreign_ace") if Path(path).name == "vault.key" else real(path))
    with pytest.raises(DropError, match="VAULT_KEY_PERMISSIONS"):
        service._vault_key(key)


def test_a_new_vault_key_is_sealed_before_its_bytes_go_in(windows, monkeypatch) -> None:
    crypto = windows / "crypto"
    crypto.mkdir(mode=0o700)
    key = crypto / "vault.key"
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, directory=False: OwnerOnlyReport(False, "unsupported"))
    with pytest.raises(DropError, match="VAULT_KEY_PERMISSIONS"):
        service._vault_key(key)
    assert not key.exists()


def test_a_write_never_opens_a_folder_on_windows(windows, monkeypatch) -> None:
    real_open = os.open
    folder = windows / "box"
    folder.mkdir(mode=0o700)

    def _open(path, flags, *args, **kwargs):
        if os.path.isdir(path):
            raise PermissionError(13, "Access is denied")  # what Windows says to os.open on a folder
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _open)
    service._write(folder / "one.wire", b"frame")
    assert (folder / "one.wire").read_bytes() == b"frame"
    assert stat.S_IMODE((folder / "one.wire").stat().st_mode) & 0o077 == 0 or os.name == "nt"


@pytest.mark.parametrize("spelled", [r"\\server\share\drop", "//server/share/drop", r"\\?\UNC\server\share\drop",
                                     r"\\.\pipe\drop"])
@pytest.mark.parametrize("what", ["home", "relay"])
def test_a_share_or_device_spelling_is_refused_before_anything_resolves_it(windows, monkeypatch, spelled, what) -> None:
    def _path(value, *rest):
        if str(value) == spelled:
            raise AssertionError("a share spelling was made into a path to look up")
        return Path(value, *rest)

    monkeypatch.setattr(service, "Path", _path)
    with pytest.raises(DropError, match=r"HOME_NOT_IN_WORKSPACE|RELAY_NOT_IN_WORKSPACE"):
        if what == "home":
            service.DropHome(spelled, windows / "relay")
        else:
            service.LocalRelay(spelled, create=True)


@pytest.mark.parametrize("name", ["drops.db", "actor.lock"])
def test_an_existing_store_file_that_is_not_owner_only_is_refused_never_repaired(windows, monkeypatch, name) -> None:
    relay = windows / "relay"
    service.LocalRelay(relay, create=True)
    service.DropHome(windows / "a", relay).close()
    real_verify = owner_only.verify_owner_only
    made: list[str] = []
    real_make = owner_only.make_owner_only
    monkeypatch.setattr(owner_only, "verify_owner_only",
                        lambda path: OwnerOnlyReport(False, "foreign_ace") if Path(path).name == name else real_verify(path))
    monkeypatch.setattr(owner_only, "make_owner_only",
                        lambda path, directory=False: made.append(Path(path).name) or real_make(path, directory=directory))
    with pytest.raises(DropError, match="UNSAFE_STORE_PERMISSIONS"):
        service.DropHome(windows / "a", relay)
    assert name not in made
