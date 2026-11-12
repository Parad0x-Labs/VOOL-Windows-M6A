"""Windows: every known secret file is made owner-only once at startup, even one VOOL never reads.

Steve round 18 (Windows 11, 12 Nov 2026, 578d185f): credentials, the A8 key, the owner identity and the
journal key were owner-only, but voolbook_token.secret (written 11 Nov, never read without a Voolbook
sign-in) still carried CodexSandboxUsers:(RX): repair-on-read never fired. The API server now runs
`core.secret_files.repair_known_secret_files` once at startup.

The Windows branch is switched on through `owner_only._SEAL_SECRETS`. Off Windows `make_owner_only`
applies 0o600, so the rows check modes.
"""
from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from core import owner_only, runtime_paths, secret_files


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool"))
    monkeypatch.delenv("VOOL_BLACKBOX_DIR", raising=False)
    runtime_paths.configure_runtime_home(tmp_path / "vool")
    monkeypatch.setattr(owner_only, "_REPAIRED", set())
    yield tmp_path / "vool"
    runtime_paths.configure_runtime_home(None)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _plant_open_files() -> list[Path]:
    """Every known secret file, written plainly and left readable, as an install from before the seal."""
    planted = []
    for path in secret_files.known_secret_files():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old secret", encoding="utf-8")
        path.chmod(0o644)
        planted.append(path)
    return planted


def test_the_known_secret_files_are_the_private_writers_files(home) -> None:
    from core.vool_wallet import WALLET_FILENAME
    from core.wallet_spend_policy_store import policy_path

    names = {path.name for path in secret_files.known_secret_files()}
    assert names == {"credentials.enc.json", "credentials.meta.json", "voolbook_token.secret",
                     "a8_digest_key.hex", "owner_identity.json", "journal.key", WALLET_FILENAME,
                     policy_path().name}
    assert all(str(path).startswith(str(runtime_paths.data_path())) for path in secret_files.known_secret_files())


def test_with_sealing_the_startup_seal_makes_every_existing_secret_owner_only(home, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    planted = _plant_open_files()
    assert secret_files.repair_known_secret_files() == {}
    for path in planted:
        assert _mode(path) == 0o600, path
        assert owner_only.verify_owner_only(path).ok, path
        assert path.read_text(encoding="utf-8") == "old secret"


def test_with_sealing_the_startup_seal_creates_nothing(home, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    assert secret_files.repair_known_secret_files() == {}
    assert not [path for path in secret_files.known_secret_files() if path.exists()]
    assert not runtime_paths.data_path("keys").exists() or not any(runtime_paths.data_path("keys").iterdir())


def test_with_sealing_a_never_read_wallet_file_is_sealed_at_startup(home, monkeypatch) -> None:
    """SA: the wallet file is the same "never read, never repaired" case as the Voolbook token."""
    from core.vool_wallet import VoolWallet

    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    wallet = VoolWallet().wallet_path
    wallet.parent.mkdir(parents=True, exist_ok=True)
    wallet.write_text("old wallet", encoding="utf-8")
    wallet.chmod(0o644)
    assert secret_files.repair_known_secret_files() == {}
    assert _mode(wallet) == 0o600
    assert owner_only.verify_owner_only(wallet).ok


def test_with_sealing_a_file_that_cannot_be_sealed_is_reported_and_boot_goes_on(home, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    _plant_open_files()
    monkeypatch.setattr(owner_only, "make_owner_only", lambda path, directory=False: owner_only.OwnerOnlyReport(False, "no_acl_support"))
    failed = secret_files.repair_known_secret_files()
    assert set(failed) == {path.name for path in secret_files.known_secret_files()}
    assert set(failed.values()) == {"no_acl_support"}


def test_without_sealing_the_startup_seal_changes_nothing(home, monkeypatch) -> None:
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", False)
    planted = _plant_open_files()
    assert secret_files.repair_known_secret_files() == {}
    assert {_mode(path) for path in planted} == {0o644}


def test_the_api_server_runs_the_startup_seal_before_it_serves() -> None:
    from apps import vool_api_server

    source = inspect.getsource(vool_api_server.main)
    assert "repair_known_secret_files()" in source
    assert source.index("repair_known_secret_files()") < source.index("app = create_app(runtime)")


def test_sabotage_without_the_startup_seal_a_never_read_token_stays_readable(home, monkeypatch) -> None:
    """The control: with only repair-on-read, a token nothing reads keeps its open mode."""
    monkeypatch.setattr(owner_only, "_SEAL_SECRETS", True)
    token = runtime_paths.data_path("voolbook_token.secret")
    token.write_text("old-token", encoding="utf-8")
    token.chmod(0o644)
    from core import onboarding

    onboarding.load_identity()  # another secret is read; the token is not
    assert _mode(token) == 0o644
    assert not owner_only.verify_owner_only(token).ok
