""""where are my keys stored?" is answered from the live credential backend, never by the model.

Capability test on Windows: the phrase had no fast path (``\\bkey\\b`` misses "keys", "where" is no
status verb, and the diagnostics regex wants "diagnostics"), so the model made up an answer. On
Windows the backend is always VOOL's encrypted vault, never the OS secret store.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.credential_store as cs
from apps.vool_agent import VoolAgent
from core import runtime_paths
from core.agent_runtime import fast_command_surface as fcs
from core.agent_runtime.fast_command_surface import (
    maybe_handle_cloud_diagnostics_intent,
    maybe_handle_key_location_intent,
)
from core.secret_storage import FORBIDDEN_CLAIM_PHRASES

PHRASES = (
    "where are my keys stored?",
    "Where are my keys stored",
    "where are my api keys stored?",
    "where are my cloud keys kept?",
    "where is my api key saved?",
    "where are my provider credentials stored?",
    "where are my credentials stored?",
    "where is my cloud key stored?",
    "where are the api keys stored?",
    "where are keys stored?",
    "where do you keep my keys?",
    "where are my keys?",
)

NOT_CLAIMED = (
    "where are my car keys",
    "where are my car keys stored?",
    "where are my house keys kept?",
    "where are the keys",
    "where are the piano keys on a keyboard?",
    "where did I leave my keys",
    "how do I store my keys safely?",
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOOL_HOME", str(tmp_path))
    runtime_paths.configure_runtime_home(tmp_path)
    yield
    runtime_paths.configure_runtime_home(None)


def _windows(monkeypatch):
    monkeypatch.delenv("VOOL_CREDENTIAL_STORE", raising=False)
    monkeypatch.setenv("VOOL_KEYCHAIN_ALLOWED", "1")
    monkeypatch.setattr(cs.sys, "platform", "win32")
    monkeypatch.setattr("core.windows_path_policy._IS_WINDOWS", True)


def test_windows_phrase_variants_name_the_vault_and_the_wallet_folder(monkeypatch):
    _windows(monkeypatch)
    assert cs.active_backend() == "vault"
    for phrase in PHRASES:
        reply = maybe_handle_key_location_intent(phrase, owner_local=True)
        assert reply is not None, phrase
        assert "encrypted local vault" in reply, phrase
        assert "file permissions" in reply, phrase
        assert "not by the OS secret store" in reply, phrase
        assert "Keys live in the OS secret store" not in reply, phrase
        assert "Keychain" not in reply, phrase
        assert "data\\keys\\" in reply, phrase
        for claim in FORBIDDEN_CLAIM_PHRASES:
            assert claim not in reply.lower(), (phrase, claim)


def test_reply_uses_the_same_sentence_as_cloud_diagnostics(monkeypatch):
    _windows(monkeypatch)
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-same-sentence-1234", label="OpenRouter")
    sentence = fcs._credential_backend_sentence("vault")
    location = maybe_handle_key_location_intent("where are my keys stored?", owner_local=True)
    diagnostics = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=True)
    assert sentence in location
    assert diagnostics is not None and sentence in diagnostics


def test_reply_never_reads_or_shows_a_secret(monkeypatch):
    _windows(monkeypatch)
    cs.store_credential("llm.cloud.openrouter", "sk-or-v1-where-stored-9911", label="OpenRouter")
    reply = maybe_handle_key_location_intent("where are my keys stored?", owner_local=True)
    assert "sk-or-v1-where-stored-9911" not in reply
    assert "9911" not in reply


def test_non_owner_gets_the_diagnostics_refusal(monkeypatch):
    _windows(monkeypatch)
    refusal = maybe_handle_cloud_diagnostics_intent("cloud diagnostics", owner_local=False)
    for phrase in PHRASES:
        assert maybe_handle_key_location_intent(phrase, owner_local=False) == refusal, phrase


def test_other_keys_are_not_claimed():
    for phrase in NOT_CLAIMED:
        assert maybe_handle_key_location_intent(phrase, owner_local=True) is None, phrase


def test_off_windows_keychain_backend_is_named(monkeypatch):
    monkeypatch.setattr("core.windows_path_policy._IS_WINDOWS", False)
    monkeypatch.setattr(cs, "active_backend", lambda: "keychain")
    reply = maybe_handle_key_location_intent("where are my keys stored?", owner_local=True)
    assert "Keys live in the macOS Keychain" in reply
    assert "data/keys/" in reply


def test_frontdoor_answers_before_the_key_status_intent_and_without_the_model(monkeypatch):
    _windows(monkeypatch)
    agent = VoolAgent(backend_name="test-backend", device="channel-test", persona_id="default")
    interpreted = SimpleNamespace(understanding_confidence=0.8, topic_hints=())
    for phrase in ("where are my keys stored?", "where is my cloud key stored?"):
        result = agent._handle_turn_frontdoor(
            raw_user_input=phrase,
            effective_input=phrase,
            normalized_input=phrase,
            source_surface="api",
            session_id="key-location-frontdoor",
            source_context={"surface": "api", "_owner_local": True},
            persona=None,
            interpreted=interpreted,
        )
        reply = (result or {}).get("result")
        assert reply is not None, phrase
        text = str(reply.get("response") or "")
        assert "encrypted local vault" in text, phrase
        assert "data\\keys\\" in text, phrase
