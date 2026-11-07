"""On Windows the node signing key material is made owner-only through core/owner_only, or not at all.

``os.chmod`` only flips the read-only bit there, so the key folder is created owner-only (its ACE is
inherited by the records written inside) and every record and the account-file protection secret is
put through ``require_owner_only``. When that cannot be reached the signer raises instead of keeping
key material under the parent's inherited ACL. These tests drive the Windows branch on any host by
setting ``_WINDOWS`` and standing in for the two owner_only calls.
"""
from __future__ import annotations

import importlib

import pytest

import network.signer as signer_mod
from core.owner_only import OwnerOnlyUnavailable


@pytest.fixture
def signer(tmp_path, monkeypatch):
    importlib.reload(signer_mod)
    signer_mod._KEY_DIR = tmp_path / "keys"
    signer_mod._LEGACY_PRIV_KEY_PATH = signer_mod._KEY_DIR / "node_signing_key.b64"
    signer_mod._KEY_RECORD_PATH = signer_mod._KEY_DIR / "node_signing_key.json"
    signer_mod._KEYRING_RECORD_PATH = signer_mod._KEY_DIR / "node_signing_key.keyring.json"
    signer_mod._KEY_ARCHIVE_DIR = signer_mod._KEY_DIR / "archive"
    signer_mod._ACCOUNT_FILE_PROTECTION_PATH = signer_mod._KEY_DIR / "key_storage.passphrase"
    signer_mod._LOCAL_KEYPAIR = None
    monkeypatch.delenv("VOOL_KEY_STORAGE_MODE", raising=False)
    monkeypatch.delenv("VOOL_KEY_PASSPHRASE", raising=False)
    monkeypatch.setattr(signer_mod, "_keyring_backend", lambda: None)
    monkeypatch.setattr(signer_mod, "_WINDOWS", True)
    yield signer_mod
    importlib.reload(signer_mod)


def _private_dirs(calls):
    def _ensure(path, *, fresh=False):
        calls.append(("dir", str(path)))
        path.mkdir(parents=True, exist_ok=True)
        return path

    return _ensure


def test_the_key_folders_are_created_owner_only_and_every_secret_file_is_protected(signer, monkeypatch):
    calls = []
    monkeypatch.setattr(signer, "ensure_private_dir", _private_dirs(calls))
    monkeypatch.setattr(signer, "require_owner_only", lambda path, *, directory=False: calls.append(("file", str(path))))

    assert signer.get_local_peer_id()
    assert ("dir", str(signer._KEY_DIR)) in calls
    assert ("dir", str(signer._KEY_ARCHIVE_DIR)) in calls
    protected = {path for kind, path in calls if kind == "file"}
    assert str(signer._ACCOUNT_FILE_PROTECTION_PATH) in protected
    assert str(signer._KEY_RECORD_PATH) in protected
    assert not signer._LEGACY_PRIV_KEY_PATH.exists()


def test_an_existing_record_is_reprotected_on_load(signer, monkeypatch):
    calls = []
    monkeypatch.setattr(signer, "ensure_private_dir", _private_dirs(calls))
    monkeypatch.setattr(signer, "require_owner_only", lambda path, *, directory=False: calls.append(("file", str(path))))
    first = signer.get_local_peer_id()
    signer._LOCAL_KEYPAIR = None
    calls.clear()

    assert signer.get_local_peer_id() == first
    assert ("file", str(signer._KEY_RECORD_PATH)) in calls


def test_no_key_material_is_kept_when_the_key_folder_cannot_be_made_owner_only(signer, monkeypatch):
    def _refuse(path, *, fresh=False):
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(signer, "ensure_private_dir", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        signer.get_local_peer_id()
    assert not signer._KEY_DIR.exists()


def test_the_signer_raises_rather_than_keep_an_unprotected_record(signer, monkeypatch):
    calls = []
    monkeypatch.setattr(signer, "ensure_private_dir", _private_dirs(calls))

    def _refuse(path, *, directory=False):
        raise OwnerOnlyUnavailable(path, "test_refusal")

    monkeypatch.setattr(signer, "require_owner_only", _refuse)
    with pytest.raises(OwnerOnlyUnavailable):
        signer.get_local_peer_id()
    assert not signer._LEGACY_PRIV_KEY_PATH.exists()
    assert signer._LOCAL_KEYPAIR is None
