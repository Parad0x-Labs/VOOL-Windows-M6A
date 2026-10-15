"""Secrets pasted into chat must not persist verbatim in the semantic-memory sink.

The conversation log already redacts; these pin that the parallel semantic-memory store
(``context_retrieval.store_turn``) also scrubs high-confidence secrets before embedding and
persisting the turn, so a pasted API key does not land in plaintext in VoolMemory + FTS.
"""
from __future__ import annotations

import pytest

import core.context_retrieval as cr
from core.context_namespace import ensure_chat_namespace
from core.memory.entries import resolve_memory_access_policy

_SECRET = "fixture-sensitive-value-814"


class _FakeMemory:
    def __init__(self):
        self.stored: list[str] = []
        self.lineages: list[str] = []

    def node_store(self, *, content, keywords, tags, context_description, embedding, lineage_request_id=""):
        self.stored.append(content)
        self.lineages.append(lineage_request_id)

    def close(self):
        pass


def test_store_turn_redacts_secret_before_persist(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(cr, "_open_memory", lambda: fake)
    monkeypatch.setattr(cr, "embed", lambda text: [0.0])
    monkeypatch.setattr(cr, "_session_scope_key", lambda sid: "sess")
    ensure_chat_namespace("sess", grant_current_receipts=False)
    policy = resolve_memory_access_policy(chat_id="sess")

    cr.store_turn(
        "sess",
        f"please remember api_key={_SECRET}",
        "noted",
        access_policy=policy,
    )

    # The turn was stored (it is a "remember" request), but the raw key is not in it.
    assert fake.stored, "high-importance turn should be persisted"
    for content in fake.stored:
        assert _SECRET not in content


@pytest.mark.parametrize("request_id", ("", "synthetic-memory-request"))
def test_redacted_semantic_write_keeps_its_request_lineage(monkeypatch, request_id):
    from core.semantic.semantic_admissions import bound_request_context

    fake = _FakeMemory()
    monkeypatch.setattr(cr, "_open_memory", lambda: fake)
    monkeypatch.setattr(cr, "embed", lambda text: [0.0])
    ensure_chat_namespace("secret-lineage", grant_current_receipts=False)
    policy = resolve_memory_access_policy(chat_id="secret-lineage")

    with bound_request_context(request_id):
        result = cr.store_turn(
            "secret-lineage",
            f"please remember api_key={_SECRET}",
            "assistant content must not become semantic memory",
            access_policy=policy,
        )

    assert result == {"status": "stored", "stored_count": 1, "reason": "eligible_user_content"}
    assert len(fake.stored) == 1 and _SECRET not in fake.stored[0]
    assert "assistant content" not in fake.stored[0]
    assert fake.lineages == [request_id]
