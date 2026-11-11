"""The post-turn fact extractor does not post to a model Ollama does not have (Steve round 16)."""
from __future__ import annotations

import logging

import pytest

from core import fact_extractor
from core.fact_extractor import FactExtractor


class _Memory:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Response:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body


@pytest.fixture
def world(monkeypatch):
    fact_extractor.reset_model_presence_for_tests()
    state = {"installed": ("qwen3:8b",), "inventory_reads": 0, "posts": [], "status": 200}

    def inventory(**kwargs):
        state["inventory_reads"] += 1
        return state["installed"]

    def post(url, json=None, timeout=None):
        state["posts"].append(url)
        return _Response(state["status"], {"message": {"content": '{"facts": []}'}})

    monkeypatch.setattr("core.local_ollama_inventory.installed_ollama_model_names", inventory)
    monkeypatch.setattr(fact_extractor.requests, "post", post)
    yield state
    fact_extractor.reset_model_presence_for_tests()


def _extractor() -> FactExtractor:
    return FactExtractor(memory=_Memory(), ollama_url="http://127.0.0.1:11434", session_id="s1")


def test_a_missing_model_is_never_posted_to(world, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="core.fact_extractor"):
        for _ in range(3):
            assert _extractor()._call_model("user: hello") == ""
    assert world["posts"] == []
    # one inventory read for all three turns, and one log line saying why
    assert world["inventory_reads"] == 1
    notes = [r.getMessage() for r in caplog.records if "not installed" in r.getMessage()]
    assert notes == ["fact extraction: model qwen3:0.6b is not installed in Ollama; extracting rule-based facts only"]


def test_rule_based_facts_still_come_through_without_the_model(world) -> None:
    applied = []
    extractor = _extractor()
    extractor._apply = lambda fact: applied.append(fact) or True
    extractor._run([{"role": "user", "content": "My project codename is QUILLBERT."}])
    assert world["posts"] == []
    assert [f.content for f in applied] == ["Active project codename: QUILLBERT"]


def test_an_installed_model_is_still_used(world) -> None:
    world["installed"] = ("qwen3:8b", "qwen3:0.6b")
    assert _extractor()._call_model("user: hello") == '{"facts": []}'
    assert world["posts"] == ["http://127.0.0.1:11434/api/chat"]


def test_a_model_name_without_a_tag_matches_latest(world) -> None:
    world["installed"] = ("tinyfacts:latest",)
    extractor = FactExtractor(memory=_Memory(), ollama_url="http://127.0.0.1:11434", model="TinyFacts")
    assert extractor._call_model("user: hi") == '{"facts": []}'


def test_a_404_marks_the_model_missing_until_the_inventory_is_read_again(world) -> None:
    world["installed"] = ("qwen3:0.6b",)
    world["status"] = 404
    assert _extractor()._call_model("user: hello") == ""
    assert _extractor()._call_model("user: hello") == ""
    assert len(world["posts"]) == 1


def test_an_empty_inventory_is_rechecked_sooner(world, monkeypatch) -> None:
    world["installed"] = ()
    clock = {"now": 1000.0}
    monkeypatch.setattr(fact_extractor.time, "monotonic", lambda: clock["now"])
    assert _extractor()._call_model("user: hello") == ""
    clock["now"] += fact_extractor._MODEL_ABSENT_UNKNOWN_TTL_SECONDS + 1
    world["installed"] = ("qwen3:0.6b",)
    assert _extractor()._call_model("user: hello") == '{"facts": []}'
    assert world["inventory_reads"] == 2


def test_the_runtime_still_builds_the_extractor_with_its_default_model() -> None:
    # The runtime passes no model=, so the default is what must be checked against the inventory.
    assert FactExtractor(memory=_Memory())._model == "qwen3:0.6b"
