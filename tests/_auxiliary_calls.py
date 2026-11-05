"""Telling a turn's answer requests from its auxiliary model calls on a served rig. Contributor: sls_0x.

Since the memory recall line landed, a chat turn in a chat that already holds stored turns makes one small
auxiliary call before the answer: the search-expansion call that writes recall search phrases
(``core.agent_runtime.turn_planner_hook.SEARCH_EXPANSION_SYSTEM_PROMPT``). It is not an answer attempt, so a check
that counts answer attempts ("the queued turn ran exactly once", "draft plus one repair") counts without it, and
checks the auxiliary calls on their own.
"""
from __future__ import annotations

from typing import Any

from core.agent_runtime.turn_planner_hook import SEARCH_EXPANSION_SYSTEM_PROMPT
from core.fact_extractor import FACT_EXTRACT_SYSTEM

#: Request paths that carry a generation. Anything else a served daemon sends to the provider is a metadata read:
#: the tool-certification identity probe posts ``/api/show`` whenever certification state is read, with no messages.
_GENERATION_PATHS = ("/api/chat", "/v1/")


def is_search_expansion(body: dict[str, Any]) -> bool:
    """Whether one captured provider request is the search-expansion call (its system prompt is exactly that one)."""
    return any(
        message.get("role") == "system" and str(message.get("content") or "") == SEARCH_EXPANSION_SYSTEM_PROMPT
        for message in (body.get("messages") or [])
    )


def is_fact_extraction(body: dict[str, Any]) -> bool:
    """Whether one captured request is the post-turn memory extraction (``core.fact_extractor``).

    The extractor runs on its own thread after a turn completes and posts straight to the native Ollama endpoint, so
    on a rig whose Ollama endpoint is the scripted provider it lands next to the answer, before or after the check.
    """
    return any(
        message.get("role") == "system" and str(message.get("content") or "") == FACT_EXTRACT_SYSTEM
        for message in (body.get("messages") or [])
    )


def is_answer_attempt(body: dict[str, Any]) -> bool:
    """Whether one captured request is an attempt at the turn's answer: a generation that is not an auxiliary lane."""
    return (
        str(body.get("path") or "").startswith(_GENERATION_PATHS)
        and not is_search_expansion(body)
        and not is_fact_extraction(body)
    )
