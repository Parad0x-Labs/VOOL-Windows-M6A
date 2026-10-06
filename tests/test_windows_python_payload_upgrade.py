"""Disposable Python-only installer transactions; no installer or product launch."""

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from installer.bundle import upgrade_python_payload as upgrade


def put(root, relative, content):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@pytest.fixture
def install(tmp_path):
    programs = tmp_path / "Local" / "Programs"
    root = programs / "VOOL"
    home = tmp_path / "Local" / "VOOL"
    put(root, "bundle_manifest.json", upgrade.encode({"schema": "vool.bundle_manifest.v1", "source_commit_full": "1" * 40}))
    put(root, "python/python.exe", b"old exe")
    put(root, "python/Lib/site-packages/pythonnet/runtime/obsolete.dll", b"owned dll")
    put(root, "python/Lib/site-packages/pythonnet-3.1.0.dist-info/METADATA", b"owned metadata")
    old = upgrade.inventory(root)
    put(root, upgrade.OWNERSHIP, upgrade.encode(old))
    candidate = tmp_path / "candidate"
    put(candidate, "bundle_manifest.json", upgrade.encode({"schema": "vool.bundle_manifest.v1", "source_commit_full": "2" * 40}))
    put(candidate, "python/python.exe", b"new exe")
    put(candidate, "python/Lib/site-packages/pythonnet-3.2.0.dist-info/METADATA", b"new metadata")
    incoming = upgrade.inventory(candidate)
    return root, programs, home, candidate, incoming


def context(install):
    root, programs, home, candidate, incoming = install
    return upgrade.Transaction(root, programs, home, incoming), candidate


def extract(candidate, root):
    # Emulates Inno file extraction, not the mutation implementation.
    for file in (candidate / "python").rglob("*"):
        if file.is_file():
            put(root, str(file.relative_to(candidate)), file.read_bytes())
    put(root, "bundle_manifest.json", (candidate / "bundle_manifest.json").read_bytes())


def test_owned_obsolete_dll_and_metadata_retired_only_after_verified_replacement(install):
    transaction, candidate = context(install)
    root, _, home, _, incoming = install
    for base in (home, root / "app/.vool_local"):
        for name in ("models/blob", "data/memory.db", "config/user.json", "WebView2/canary", "workspace/note"):
            put(base, name, b"protected")
    transaction.check()
    transaction.begin()
    assert not (root / "python").exists()
    extract(candidate, root)
    transaction.verify()
    recovery = transaction.finish()
    assert upgrade.inventory(root) == incoming
    assert not (root / "python/Lib/site-packages/pythonnet/runtime/obsolete.dll").exists()
    assert (recovery / "previous/Lib/site-packages/pythonnet/runtime/obsolete.dll").read_bytes() == b"owned dll"
    assert json.loads((root / upgrade.OWNERSHIP).read_text()) == incoming
    assert not (root / upgrade.ACTIVE).exists()
    for base in (home, root / "app/.vool_local"):
        assert all(p.read_bytes() == b"protected" for p in base.rglob("*") if p.is_file())


@pytest.mark.parametrize("kind", ["fresh", "repair", "upgrade"])
def test_fresh_repair_and_upgrade(install, kind):
    transaction, candidate = context(install)
    root = install[0]
    if kind == "fresh":
        # Fresh fixture is a separate empty program root; existing fixture preserved.
        root = install[1] / "Fresh" / "VOOL"
        transaction = upgrade.Transaction(root, root.parent, install[2], install[4])
    elif kind == "repair":
        candidate = root
        transaction = upgrade.Transaction(root, install[1], install[2], upgrade.inventory(root))
    files = {str(p.relative_to(candidate)): p.read_bytes() for p in (candidate / "python").rglob("*") if p.is_file()}
    manifest = (candidate / "bundle_manifest.json").read_bytes()
    transaction.begin()
    for path, data in files.items():
        put(root, path, data)
    put(root, "bundle_manifest.json", manifest)
    transaction.verify()
    transaction.finish()


@pytest.mark.parametrize("reason", ["unknown", "modified", "missing_inventory", "binding"])
def test_ambiguous_ownership_refused_without_mutation(install, reason):
    transaction, _ = context(install)
    root = install[0]
    if reason == "unknown":
        put(root, "python/Lib/site-packages/custom.dll", b"custom")
    elif reason == "modified":
        put(root, "python/python.exe", b"custom modified executable")
    elif reason == "missing_inventory":
        (root / upgrade.OWNERSHIP).rename(root / "saved-inventory")
    else:
        put(root, "bundle_manifest.json", b"different installation")
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(upgrade.UpgradeError):
        transaction.begin()
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (root / upgrade.ACTIVE).exists()


@pytest.mark.parametrize("failure", ["partial_extraction", "bad_hash", "migration", "cancellation"])
def test_failure_preserves_and_restores_previous_python(install, failure):
    transaction, candidate = context(install)
    root = install[0]
    previous = (root / "python/python.exe").read_bytes()
    ownership = (root / upgrade.OWNERSHIP).read_bytes()
    transaction.begin()
    if failure != "cancellation":
        extract(candidate, root)
    if failure == "partial_extraction":
        (root / "python/Lib/site-packages/pythonnet-3.2.0.dist-info/METADATA").unlink()
    if failure == "bad_hash":
        put(root, "python/python.exe", b"bad extraction")
    if failure in ("partial_extraction", "bad_hash"):
        with pytest.raises(upgrade.UpgradeError):
            transaction.verify()
    recovery = transaction.recover()
    assert (root / "python/python.exe").read_bytes() == previous
    assert (root / upgrade.OWNERSHIP).read_bytes() == ownership
    assert recovery.exists()
    assert not (root / upgrade.ACTIVE).exists()


def test_interrupted_prepare_before_rename_recovers(install, monkeypatch):
    transaction, _ = context(install)
    original = upgrade.os.replace

    def fail(source, target):
        if Path(source) == install[0] / "python":
            raise OSError("injected rename failure")
        return original(source, target)

    monkeypatch.setattr(upgrade.os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        transaction.begin()
    monkeypatch.setattr(upgrade.os, "replace", original)
    transaction.recover()
    transaction.check()


def test_committed_interruption_never_reverts_accepted_payload(install, monkeypatch):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    original = transaction._archive
    monkeypatch.setattr(transaction, "_archive", lambda: (_ for _ in ()).throw(OSError("archive interrupted")))
    with pytest.raises(OSError, match="archive interrupted"):
        transaction.finish()
    monkeypatch.setattr(transaction, "_archive", original)
    transaction.recover()
    assert (install[0] / "python/python.exe").read_bytes() == b"new exe"


@pytest.mark.parametrize("path", ["../data/x", "/abs", "C:/x", "\\\\server\\x", "a:b", "a/../x", "a./x", "CON", "a\\x"])
def test_invalid_inventory_paths_fail_before_writing(install, path):
    incoming = {**install[4], "files": {path: {"size": 1, "sha256": "a" * 64}}}
    with pytest.raises(upgrade.UpgradeError):
        upgrade.Transaction(install[0], install[1], install[2], incoming).begin()
    assert not (install[0] / upgrade.ACTIVE).exists()


def test_wrong_root_or_home_overlap_refused(install):
    root, programs, _, _, incoming = install
    for target, home in ((root.parent / "Sibling", root.parent / "Home"), (root, root / "profile"), (root, programs)):
        with pytest.raises(upgrade.UpgradeError):
            upgrade.Transaction(target, programs, home, incoming).begin()
    assert not (root / upgrade.ACTIVE).exists()


def test_running_runtime_refused(install, monkeypatch):
    transaction, _ = context(install)
    monkeypatch.setattr(upgrade, "runtime_running", lambda: True)
    with pytest.raises(upgrade.UpgradeError, match="Close VOOL"):
        transaction.begin()
    assert not (install[0] / upgrade.ACTIVE).exists()


def test_manifest_binds_every_byte_and_build(install):
    payload = install[4]
    assert payload["bundle_manifest_sha256"] == hashlib.sha256((install[3] / "bundle_manifest.json").read_bytes()).hexdigest()
    assert all(len(record["sha256"]) == 64 for record in payload["files"].values())


def test_arbitrary_non_vool_build_marker_cannot_authorize_inventory(install):
    put(install[3], "bundle_manifest.json", b'{"source_commit_full":"not-a-build"}')
    with pytest.raises(upgrade.UpgradeError, match="Valid VOOL"):
        upgrade.inventory(install[3])


def test_check_is_read_only_and_pending_transaction_is_not_ignored(install):
    transaction, _ = context(install)
    before = sorted(p.relative_to(install[0]).as_posix() for p in install[0].rglob("*"))
    transaction.check()
    assert sorted(p.relative_to(install[0]).as_posix() for p in install[0].rglob("*")) == before
    transaction.begin()
    with pytest.raises(upgrade.UpgradeError, match="Pending"):
        transaction.check()


@pytest.mark.parametrize("checkpoint", ["retired", "extracted", "ownership_written", "restored"])
def test_restart_recovers_each_uncommitted_checkpoint(install, checkpoint, monkeypatch):
    transaction, candidate = context(install)
    transaction.begin()
    if checkpoint in ("extracted", "ownership_written", "restored"):
        extract(candidate, install[0])
    if checkpoint == "ownership_written":
        # Power loss between ownership-marker replacement and committed journal update.
        put(install[0], upgrade.OWNERSHIP, upgrade.encode(install[4]))
    elif checkpoint == "restored":
        original = transaction._archive
        monkeypatch.setattr(transaction, "_archive", lambda: (_ for _ in ()).throw(OSError("archive interrupted")))
        with pytest.raises(OSError):
            transaction.recover()
        monkeypatch.setattr(transaction, "_archive", original)
    restarted, _ = context(install)
    restarted.recover()
    restarted.check()
    assert (install[0] / "python/python.exe").read_bytes() == b"old exe"
    assert not (install[0] / upgrade.ACTIVE).exists()


def test_changed_backup_refuses_recovery_and_preserves_both_trees(install):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    put(install[0], upgrade.ACTIVE + "/previous/python.exe", b"changed backup")
    with pytest.raises(upgrade.UpgradeError, match="Previous payload changed"):
        transaction.recover()
    assert (install[0] / "python/python.exe").read_bytes() == b"new exe"
    assert (install[0] / upgrade.ACTIVE / "previous/python.exe").read_bytes() == b"changed backup"


@pytest.mark.parametrize("target_kind", ["journal", "ownership"])
def test_interrupted_metadata_write_fails_closed_without_mutating_payload(install, monkeypatch, target_kind):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    original = upgrade.os.replace

    def fail(source, target):
        intended = transaction.active / "journal.json" if target_kind == "journal" else install[0] / upgrade.OWNERSHIP
        if Path(target) == intended:
            raise OSError("power loss during journal replacement")
        return original(source, target)

    monkeypatch.setattr(upgrade.os, "replace", fail)
    with pytest.raises(OSError, match="power loss"):
        transaction.finish()
    monkeypatch.setattr(upgrade.os, "replace", original)
    with pytest.raises(upgrade.UpgradeError, match="Interrupted metadata write; manual recovery review required"):
        transaction.recover()
    assert (install[0] / "python/python.exe").read_bytes() == b"new exe"
    assert (transaction.active / "previous/python.exe").read_bytes() == b"old exe"
    pending = transaction.active / "journal.json.pending" if target_kind == "journal" else install[0] / (upgrade.OWNERSHIP + ".pending")
    assert pending.exists()


def test_real_migration_conflict_restores_python_and_keeps_all_user_state(install):
    from installer.bundle.migrate_install_state import MigrationError, migrate_legacy_home

    transaction, candidate = context(install)
    root, _, home, _, _ = install
    legacy = root / "app/.vool_local"
    put(legacy, "data/memory.db", b"legacy database")
    put(home, "data/memory.db", b"current database")
    put(home, "models/model-canary", b"preserved model")
    put(home, "config/provider-canary", b"preserved configuration")
    ownership = (root / upgrade.OWNERSHIP).read_bytes()
    transaction.begin()
    extract(candidate, root)
    transaction.verify()
    with pytest.raises(MigrationError, match="conflict"):
        migrate_legacy_home(root, home)
    recovery = transaction.recover()
    assert (root / "python/python.exe").read_bytes() == b"old exe"
    assert (root / upgrade.OWNERSHIP).read_bytes() == ownership
    assert (legacy / "data/memory.db").read_bytes() == b"legacy database"
    assert (home / "data/memory.db").read_bytes() == b"current database"
    assert (home / "models/model-canary").read_bytes() == b"preserved model"
    assert (home / "config/provider-canary").read_bytes() == b"preserved configuration"
    assert (recovery / "failed/python.exe").read_bytes() == b"new exe"


def test_real_migration_success_precedes_ownership_finalization(install):
    from installer.bundle.migrate_install_state import migrate_legacy_home

    transaction, candidate = context(install)
    root, _, home, _, incoming = install
    put(root, "app/.vool_local/data/memory.db", b"legacy database")
    transaction.begin()
    extract(candidate, root)
    transaction.verify()
    assert read_manifest(root) != incoming
    assert migrate_legacy_home(root, home) == 1
    transaction.finish()
    assert (home / "data/memory.db").read_bytes() == b"legacy database"
    assert read_manifest(root) == incoming


def read_manifest(root):
    return json.loads((root / upgrade.OWNERSHIP).read_text())


def test_verified_retained_material_allows_reinstall_without_deleting_it(install):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    archive = transaction.finish()
    # Emulate removal of Inno-tracked payload in this disposable fixture only.
    import shutil

    shutil.rmtree(install[0] / "python")
    (install[0] / "bundle_manifest.json").unlink()
    before = {p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    transaction.check()
    transaction.begin()
    extract(candidate, install[0])
    transaction.finish()
    assert {p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before
    assert (install[0] / "python/python.exe").read_bytes() == b"new exe"


def test_fresh_cancel_with_only_verified_recovery_material_can_retry(install):
    root = install[1] / "Fresh/VOOL"
    transaction = upgrade.Transaction(root, root.parent, install[2], install[4])
    transaction.begin()
    transaction.recover()
    transaction.check()
    transaction.begin()
    transaction.recover()


def test_unowned_post_uninstall_residue_still_refused(install):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    transaction.finish()
    import shutil

    shutil.rmtree(install[0] / "python")
    (install[0] / "bundle_manifest.json").unlink()
    put(install[0], "custom-note", b"must not be overwritten or deleted")
    with pytest.raises(upgrade.UpgradeError, match="Existing installation"):
        transaction.begin()
    assert (install[0] / "custom-note").read_bytes() == b"must not be overwritten or deleted"


def test_dangling_ownership_link_cannot_evade_reinstall_guard(install, tmp_path):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    transaction.finish()
    import shutil

    shutil.rmtree(install[0] / "python")
    (install[0] / "bundle_manifest.json").unlink()
    (install[0] / upgrade.OWNERSHIP).unlink()
    target = tmp_path / "absent-ownership"
    try:
        os.symlink(target, install[0] / upgrade.OWNERSHIP)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable")
    with pytest.raises(upgrade.UpgradeError, match="Reparse"):
        transaction.begin()
    assert not target.exists()
    assert not (install[0] / upgrade.ACTIVE).exists()


def test_interrupted_marker_restore_requires_review_and_preserves_both_payloads(install, monkeypatch):
    transaction, candidate = context(install)
    transaction.begin()
    extract(candidate, install[0])
    original = upgrade.os.replace

    def fail(source, target):
        if Path(source).name == "old-ownership.json.restore":
            raise OSError("power loss during marker restore")
        return original(source, target)

    monkeypatch.setattr(upgrade.os, "replace", fail)
    with pytest.raises(OSError, match="power loss"):
        transaction.recover()
    monkeypatch.setattr(upgrade.os, "replace", original)
    with pytest.raises(upgrade.UpgradeError, match="Interrupted metadata write; manual recovery review required"):
        transaction.recover()
    assert (transaction.active / "old-ownership.json.restore").exists()
    assert (install[0] / "python/python.exe").read_bytes() == b"old exe"
    assert (transaction.active / "failed/python.exe").read_bytes() == b"new exe"


def test_hard_linked_owned_file_refused_without_touching_external(install, tmp_path):
    source = install[0] / "python/python.exe"
    linked = tmp_path / "external-executable"
    os.link(source, linked)
    transaction, _ = context(install)
    with pytest.raises(upgrade.UpgradeError, match="hard link"):
        transaction.begin()
    assert linked.read_bytes() == b"old exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and mutex guards")
def test_junction_in_owned_subtree_refused(install, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    put(outside, "canary", b"unchanged")
    junction = install[0] / "python/escape"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    transaction, _ = context(install)
    with pytest.raises(upgrade.UpgradeError, match="Reparse"):
        transaction.begin()
    assert (outside / "canary").read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex guard")
def test_real_windows_mutex_presence_is_detected(monkeypatch):
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    names = tuple(r"Local\VOOL_PYTHON_UPGRADE_TEST_" + uuid.uuid4().hex for _ in range(2))
    monkeypatch.setattr(upgrade, "MUTEX_NAMES", names)
    assert not upgrade.runtime_running()
    for name in names:
        handle = kernel.CreateMutexW(None, False, name)
        assert handle
        try:
            assert upgrade.runtime_running()
        finally:
            kernel.CloseHandle(handle)
        assert not upgrade.runtime_running()


@pytest.mark.skipif(os.name != "nt", reason="Windows path guards")
@pytest.mark.parametrize("target", [r"\\server\share\VOOL", r"\\?\C:\VOOL", r"\\.\C:\VOOL", r"C:VOOL"])
def test_windows_unc_device_and_drive_relative_roots_refused(install, target):
    with pytest.raises(upgrade.UpgradeError):
        upgrade.Transaction(Path(target), install[1], install[2], install[4]).begin()
    assert not (install[0] / upgrade.ACTIVE).exists()


def test_inno_orders_guard_transaction_verification_migration_and_finalization():
    script = (Path(__file__).resolve().parents[1] / "installer/bundle/vool.iss").read_text()
    assert "SetupMutex=Local\\VOOL_PYTHON_PAYLOAD_UPGRADE" in script
    assert "python-upgrade-bootstrap" in script
    assert "'-I -S -B" in script
    assert "Check: PythonUpgradeReady" in script
    post = script[script.index("procedure CurStepChanged"):]
    assert post.index("'begin'") < post.index("'verify'") < post.index("migrate_install_state.py") < post.index("'finish'")
    assert "procedure DeinitializeSetup" in script and "'recover'" in script
    assert "[InstallDelete]" not in script
