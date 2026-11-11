"""B14: `cloud diagnostics` must name the store the keys are really in.

On Windows (and anywhere the macOS Keychain is not active) keys sit in VOOL's AES vault, whose
sealing key lives in the same user profile (core.secret_storage account-file-permissions class).
The reply used to end with "Keys live in the OS secret store" whatever the backend.
"""
from __future__ import annotations

import pytest

import core.credential_store as cs
from core import runtime_paths
from core.agent_runtime.fast_command_surface import maybe_handle_cloud_diagnostics_intent
from core.secret_storage import FORBIDDEN_CLAIM_PHRASES


class _FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, account, password):
        self._store[(service, account)] = password

    def get_password(self, service, account):
        return self._store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self._store:
            raise self.errors.PasswordDeleteError("not found")
        del self._store[(service, account)]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def _assert_no_overclaim(reply: str) -> None:
    lowered = reply.lower()
    for phrase in FORBIDDEN_CLAIM_PHRASES:
        assert phrase not in lowered, phrase


def test_vault_backend_reply_does_not_claim_the_os_secret_store(monkeypatch):
    monkeypatch.setenv("VOOL_CREDENTIAL_STORE", "vault")
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-vault-truth-4321", label="OpenRouter")
    assert cs.active_backend() == "vault"

    reply = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)

    assert reply is not None
    assert "sk-or-v1-vault-truth-4321" not in reply
    assert "…4321" in reply
    assert "Keys live in the OS secret store" not in reply
    assert "Keychain" not in reply
    assert "encrypted local vault" in reply
    assert "file permissions" in reply
    _assert_no_overclaim(reply)


def test_windows_platform_reply_names_the_vault(monkeypatch):
    # Windows never selects the Keychain (credential_store._load_keyring is macOS-only), even with
    # the grant set and a keyring module importable.
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setenv("VOOL_KEYCHAIN_ALLOWED", "1")
    monkeypatch.setattr(cs.sys, "platform", "win32")
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-windows-8765", label="OpenRouter")
    assert cs.active_backend() == "vault"

    reply = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)

    assert reply is not None
    assert "sk-or-v1-windows-8765" not in reply
    assert "Keys live in the OS secret store" not in reply
    assert "encrypted local vault" in reply
    _assert_no_overclaim(reply)


def test_keychain_backend_reply_names_the_keychain(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(cs, "_active_keyring", lambda *args, **kwargs: fake)
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-keychain-2468", label="OpenRouter")

    reply = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)

    assert reply is not None
    assert "sk-or-v1-keychain-2468" not in reply
    assert "Keys live in the macOS Keychain" in reply
    assert "encrypted local vault" not in reply
