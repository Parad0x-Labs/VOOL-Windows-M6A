"""Where keys live is said only for a backend VOOL knows; any other backend is named, never guessed.

Chat answered "where are my keys stored?" and ``cloud diagnostics`` from the live backend, but every
backend that was not "keychain" was described as VOOL's encrypted local vault. A new backend (for
example a Windows Credential Manager store) would then be described as the file vault. The vault
wording is now kept for the "vault" backend only; an unrecognised backend is reported by its own name
with no claim about where or how it stores keys.
"""
from __future__ import annotations

import pytest

import core.credential_store as cs
from core import runtime_paths
from core.agent_runtime import fast_command_surface as fcs
from core.agent_runtime.fast_command_surface import (
    maybe_handle_cloud_diagnostics_intent,
    maybe_handle_key_location_intent,
)

UNKNOWN = "windows-credential-manager"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def _no_location_claim(text: str) -> None:
    for claim in ("vault", "Keychain", "file permissions", "OS secret store", "user profile"):
        assert claim not in text, (claim, text)


def test_an_unknown_backend_is_named_without_a_location_claim() -> None:
    sentence = fcs._credential_backend_sentence(UNKNOWN)
    label = fcs._credential_backend_label(UNKNOWN)
    assert UNKNOWN in sentence and UNKNOWN in label
    _no_location_claim(sentence)
    _no_location_claim(label)


@pytest.mark.parametrize("backend", ["", None, "VAULT", "keychain2"])
def test_an_empty_or_misspelled_backend_is_not_called_the_vault(backend) -> None:
    _no_location_claim(fcs._credential_backend_sentence(backend))
    _no_location_claim(fcs._credential_backend_label(backend))


def test_the_known_backends_keep_their_wording() -> None:
    assert fcs._credential_backend_sentence("keychain") == "Keys live in the macOS Keychain"
    assert "encrypted local vault" in fcs._credential_backend_sentence("vault")
    assert fcs._credential_backend_label("keychain") == "the macOS Keychain"
    assert fcs._credential_backend_label("vault") == "the encrypted local vault"


def test_chat_answers_read_the_live_backend(monkeypatch) -> None:
    monkeypatch.setattr(cs, "active_backend", lambda: UNKNOWN)
    reply = maybe_handle_key_location_intent("where are my keys stored?", owner_local=True)
    assert reply is not None and UNKNOWN in reply
    _no_location_claim(reply.split("If you mean your wallet")[0])
    monkeypatch.setattr(cs, "export_diagnostics", lambda: {"backend": UNKNOWN, "credentials": [
        {"name": "llm.cloud.openrouter", "label": "OpenRouter", "masked_suffix": "…1234"}]})
    diagnostics = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)
    assert diagnostics is not None and UNKNOWN in diagnostics
    _no_location_claim(diagnostics)
