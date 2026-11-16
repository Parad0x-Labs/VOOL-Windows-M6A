"""Windows: the X-not-connected line stands down for a publish ask and, with Web lookups on, a public X search.

Windows CI run 43911900481 (0.7.3 new reds, batch 3): the line answered "Can you publish this thread for
me on X?" (tests/test_x_editorial_served.py expects the turn's own "cannot publish" answer) and "check
Toly on X" (tests/test_alpha_semantic_context_smoke.py expects the web lookup). Publishing does not exist
in the X lane, so keys would not help; a public X search is answered by the web lane while Web lookups
are on. With Web lookups off or Local Only on a search still gets the line, and an ask that only the X
tools answer ("read my latest posts on X") gets it with Web lookups on, as before.
"""
from __future__ import annotations

import pytest

from core import windows_write_gate as gate
from core.agent_runtime.windows_x_not_connected import (
    X_NOT_CONNECTED,
    X_NOT_CONNECTED_LOCAL_ONLY,
    maybe_handle_x_not_connected,
)


def _web(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    import core.policy_engine as policy_engine

    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda *args, **kwargs: on)


def _local_only(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    import core.policy_engine as policy_engine

    monkeypatch.setattr(policy_engine, "local_only_mode", lambda *args, **kwargs: on)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    _local_only(monkeypatch, False)
    return monkeypatch


@pytest.mark.parametrize("web_on", [False, True], ids=["web-off", "web-on"])
@pytest.mark.parametrize(
    "text", ["Can you publish this thread for me on X?", "schedule this thread on X for tomorrow"]
)
def test_a_publish_or_schedule_ask_is_not_answered_with_the_line(windows, text, web_on) -> None:
    _web(windows, web_on)
    assert maybe_handle_x_not_connected(text) is None


@pytest.mark.parametrize("text", ["check Toly on X", "check Tolyy on X in Solana", "search X for solana news"])
def test_a_public_x_search_goes_to_the_web_lane_while_web_lookups_are_on(windows, text) -> None:
    _web(windows, True)
    assert maybe_handle_x_not_connected(text) is None


@pytest.mark.parametrize("text", ["check Toly on X", "search X for solana news"])
def test_a_public_x_search_gets_the_line_with_web_lookups_off(windows, text) -> None:
    _web(windows, False)
    assert maybe_handle_x_not_connected(text) == X_NOT_CONNECTED


def test_a_public_x_search_under_local_only_gets_the_local_only_line(windows) -> None:
    _web(windows, True)
    _local_only(windows, True)
    assert maybe_handle_x_not_connected("check Toly on X") == X_NOT_CONNECTED_LOCAL_ONLY


@pytest.mark.parametrize("web_on", [False, True], ids=["web-off", "web-on"])
def test_an_ask_only_the_x_tools_answer_still_gets_the_line(windows, web_on) -> None:
    _web(windows, web_on)
    assert maybe_handle_x_not_connected("read my latest posts on X") == X_NOT_CONNECTED


def test_off_windows_nothing_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    _web(monkeypatch, False)
    for text in ("check Toly on X", "read my latest posts on X", "Can you publish this thread for me on X?"):
        assert maybe_handle_x_not_connected(text) is None
