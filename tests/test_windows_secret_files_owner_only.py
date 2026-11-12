"""Windows: every secret file VOOL writes is owner-only, and a write that cannot be sealed writes nothing.

Claims sweep on Windows 11 (11 Nov 2026, 170f6572): credentials.enc.json, voolbook_token.secret,
a8_digest_key.hex, owner_identity.json and blackbox\\journal.key each inherited
`CodexSandboxUsers:(RX)` from %LOCALAPPDATA%. Only keys\\* were sealed (network/signer.py). Each of
these writers now seals its file through `core.owner_only` on Windows: an empty sibling is made
owner-only before the secret is written into it, so the bytes are never readable by another account,
and when it cannot be made owner-only nothing is written. A file written before this is repaired
the first time it is read.

These rows drive the Windows branch on any host through `owner_only._SEAL_SECRETS`. Off Windows,
`make_owner_only` applies `0o600` and checks the owner, so the rows check those modes here. On
Windows the same calls set and verify the protected one-ACE DACL. A spy proves the seal comes
before the secret: every sealed file was still empty when it was sealed.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core import owner_only, runtime_paths


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool"))
    runtime_paths.configure_runtime_home(tmp_path / "vool")
    yield tmp_path / "vool"
    runtime_paths.configure_runtime_home(None)


@pytest.fixture
def sealing(monkeypatch):
    """The Windows branch switched on, with a record of every seal and the size of what it sealed."""
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "_REPAIRED", set())
    sealed: list[tuple[str, int]] = []
    real = owner_only.make_owner_only

    def spy(path, *, directory=False):
        target = os.fspath(path)
        sealed.append((Path(target).name, os.path.getsize(target)))
        return real(path, directory=directory)

    monkeypatch.setattr(owner_only, "make_owner_only", spy)
    return sealed


@pytest.fixture
def refusing(monkeypatch):
    """The Windows branch switched on, on a volume where nothing can be made owner-only."""
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "_REPAIRED", set())
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support"))


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _write_credentials() -> Path:
    from core import credential_store

    credential_store._save_raw({"demo": {"nonce": "n", "ciphertext": "c"}})
    credential_store._meta_save({"demo": {"label": "x", "created": "t"}})
    return credential_store._store_path()


def _write_voolbook_token() -> Path:
    from core.voolbook_identity import save_token_locally

    return save_token_locally("vb-token-secret-value")


def _write_a8_key() -> Path:
    from core import finalization

    finalization._a8_digest_key_cache = None
    finalization._a8_digest_key()
    return runtime_paths.data_path("a8_digest_key.hex")


def _write_owner_identity() -> Path:
    from core import onboarding

    return onboarding.save_identity(agent_name="Helper", privacy_pact="local only", owner_note="")


def _write_journal_key() -> Path:
    from storage.blackbox.journal import Journal

    journal = Journal(runtime_paths.data_path("blackbox"))
    journal.key()
    return journal.key_path


def _write_wallet_file() -> Path:
    from core import vool_wallet

    target = runtime_paths.data_path("wallet", "wallet.json")
    vool_wallet._atomic_write_private(target, json.dumps({"secret": "seed"}))
    return target


WRITERS = {
    "credentials.enc.json": _write_credentials,
    "voolbook_token.secret": _write_voolbook_token,
    "a8_digest_key.hex": _write_a8_key,
    "owner_identity.json": _write_owner_identity,
    "journal.key": _write_journal_key,
    "wallet.json": _write_wallet_file,
}


@pytest.mark.parametrize("name", list(WRITERS))
def test_with_sealing_each_secret_is_owner_only_and_sealed_while_empty(home, sealing, name) -> None:
    path = WRITERS[name]()
    assert path.name == name
    assert path.read_bytes()
    assert owner_only.verify_owner_only(path).ok
    assert _mode(path) == 0o600
    pending = [size for sealed_name, size in sealing if sealed_name.startswith(f".{name}.")]
    assert pending, f"{name} was never sealed before it was written: {sealing}"
    assert set(pending) == {0}, f"{name} was sealed only after secret bytes were in it: {sealing}"
    assert not [entry for entry in path.parent.iterdir() if entry.name.endswith(".tmp")]


@pytest.mark.parametrize("name", list(WRITERS))
def test_with_sealing_a_secret_that_cannot_be_sealed_is_not_written(home, refusing, name) -> None:
    with pytest.raises(owner_only.OwnerOnlyUnavailable):
        WRITERS[name]()
    data = runtime_paths.data_path()
    left = [str(entry.relative_to(data)) for entry in data.rglob("*") if entry.is_file() and entry.stat().st_size]
    assert not [entry for entry in left if name in entry], left


def test_with_sealing_an_existing_secret_is_repaired_when_it_is_read(home, sealing) -> None:
    from core import onboarding
    from core.voolbook_identity import load_local_token

    token = runtime_paths.data_path("voolbook_token.secret")
    token.write_text("old-token", encoding="utf-8")
    token.chmod(0o644)
    identity = runtime_paths.data_path("owner_identity.json")
    identity.write_text(json.dumps({"agent_name": "Helper"}), encoding="utf-8")
    identity.chmod(0o644)

    assert load_local_token() == "old-token"
    assert onboarding.load_identity()["agent_name"] == "Helper"
    assert _mode(token) == 0o600
    assert _mode(identity) == 0o600


def test_without_sealing_the_writers_keep_their_own_handling(home, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", False)
    monkeypatch.setattr(owner_only, "write_private_file", lambda *a, **k: pytest.fail("sealed off Windows"))
    for writer in WRITERS.values():
        assert writer().read_bytes()
    assert owner_only.repair_private_file(runtime_paths.data_path("voolbook_token.secret")).ok


def test_sabotage_a_plain_write_leaves_the_secret_readable(home, monkeypatch) -> None:
    """The control the rows above depend on: without sealing, a written file keeps the open mode."""
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    monkeypatch.setattr(owner_only, "write_private_file", lambda path, data, encoding="utf-8": Path(path).write_text(str(data), encoding=encoding) and Path(path))
    previous = os.umask(0o022)
    try:
        path = _write_voolbook_token()
    finally:
        os.umask(previous)
    assert _mode(path) == 0o644
    assert not owner_only.verify_owner_only(path).ok
