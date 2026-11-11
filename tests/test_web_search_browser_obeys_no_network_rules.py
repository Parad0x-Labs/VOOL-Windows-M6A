"""The headless browser behind web search obeys the same no-network rules as every other provider.

Finding Edge on Windows turns a new process with network access on for every Windows web search.
The browser is not an HTTP request, so it never passes `core.remote_fetch_policy.open_remote`, and
its doors asked only the explicit per-turn veto: under Local Only the HTTP providers of a search
were refused while the browser still started twice (DuckDuckGo, then Bing). Each row here forces
the Windows seam with Edge in Program Files, spies on process creation, and runs a real
`web_research` call. Nothing opens a socket: every name lookup fails, and the spy starts nothing.

Private chat is not a row: it keeps a chat out of every store, and it does not refuse web search.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from core import auto_local_only_mode, effect_gateway, policy_engine, windows_known_folders
from core import remote_fetch_policy as rfp
from tools.browser import browser_render as br
from tools.web import web_research

_EDGE = ("Microsoft", "Edge", "Application", "msedge.exe")
_QUERY = "latest news about jupiter"


@pytest.fixture()
def windows_edge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    for name in ("VOOL_BROWSER_BINARY", "VOOL_BROWSER_ENGINE", "WEB_SEARCH_PROVIDER_ORDER", "PLAYWRIGHT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALLOW_BROWSER_FALLBACK", "1")
    monkeypatch.setattr(br.sys, "platform", "win32")
    program_files = tmp_path / "Program Files (x86)"
    exe = program_files.joinpath(*_EDGE)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    exe.chmod(0o755)
    monkeypatch.setattr(windows_known_folders, "program_files_folders", lambda: [str(program_files)])
    br.reset_browser_binary_cache_for_test()

    # Offline: no name resolves, so nothing can leave even if a check were missing.
    def _no_dns(*_args, **_kwargs):
        raise socket.gaierror("offline test")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    launches: list[list[str]] = []

    def _spy(argv, *_args, **_kwargs):
        launches.append(list(argv))
        raise OSError("spy: no process is started in this test")

    monkeypatch.setattr(subprocess, "Popen", _spy)
    # Only the browser provider is in the chain, so the row is about the browser alone.
    monkeypatch.setattr(web_research, "_configured_provider_order", lambda: ["browser_search"])
    monkeypatch.setattr(web_research, "_prefer_specialized_live_research", lambda _q: False)
    yield {"edge": str(exe), "launches": launches}
    br.reset_browser_binary_cache_for_test()


def _search(context: dict) -> tuple[web_research.ResearchResult, dict]:
    with rfp.remote_fetch_policy_scope(context):
        result = web_research.web_research(_QUERY, total_budget_s=60.0)
    return result, context


def _denied_hosts(context: dict) -> list[str]:
    receipts = context.get(effect_gateway.EFFECT_RECEIPTS_CONTEXT_KEY) or []
    return [str(row.get("host")) for row in receipts if row.get("decision") == "denied"]


def test_control_with_nothing_refused_the_spy_sees_edge_launched(windows_edge: dict) -> None:
    # Proves the spy and the seam: the same setup does reach Edge when no rule refuses it.
    _search({})
    launches = windows_edge["launches"]
    assert launches, "the control row never reached the browser, so the other rows prove nothing"
    assert all(argv[0] == windows_edge["edge"] for argv in launches)


def test_local_only_policy_starts_no_browser(windows_edge: dict) -> None:
    result, context = _search({"local_only_mode": True})
    assert windows_edge["launches"] == []
    assert any("duckduckgo.com" in host or "bing.com" in host for host in _denied_hosts(context)), result.notes


def test_local_only_selected_for_the_turn_starts_no_browser(windows_edge: dict) -> None:
    _search({auto_local_only_mode.CONTEXT_KEY: True})
    assert windows_edge["launches"] == []


def test_the_auto_local_only_ambient_scope_starts_no_browser(windows_edge: dict) -> None:
    with auto_local_only_mode.local_only_egress_scope():
        _search({})
    assert windows_edge["launches"] == []


def test_a_machine_set_to_local_only_starts_no_browser(windows_edge: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: True)
    _search({})
    assert windows_edge["launches"] == []


def test_the_explicit_per_turn_veto_starts_no_browser(windows_edge: dict) -> None:
    result, _context = _search({"allow_remote_fetch": False})
    assert windows_edge["launches"] == []
    assert "remote_fetch_disabled" in result.notes


def test_a_permission_gateway_denial_starts_no_browser(windows_edge: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        effect_gateway, "decide_network_fetch", lambda _ctx=None: (effect_gateway.DECISION_DENIED, "test mode denies the web")
    )
    _result, context = _search({})
    assert windows_edge["launches"] == []
    assert _denied_hosts(context), "a gateway denial left no receipt"


def test_browser_search_switched_off_by_policy_starts_no_browser(windows_edge: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_BROWSER_FALLBACK", raising=False)
    monkeypatch.setattr(policy_engine, "allow_browser_fallback", lambda: False)
    result, _context = _search({})
    assert windows_edge["launches"] == []
    assert "browser_search_skipped:disabled_by_policy" in result.notes


def test_web_fallback_off_in_policy_starts_no_browser(windows_edge: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped default: system.allow_web_fallback is false, and web.allow_browser_fallback
    # needs it, so a launcher that does not set ALLOW_BROWSER_FALLBACK gets no browser.
    monkeypatch.delenv("ALLOW_BROWSER_FALLBACK", raising=False)
    monkeypatch.setattr(policy_engine, "allow_web_fallback", lambda: False)
    result, _context = _search({})
    assert windows_edge["launches"] == []
    assert "browser_search_skipped:disabled_by_policy" in result.notes


def test_the_page_render_door_obeys_local_only_too(windows_edge: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    # browser_render is the door "summarize this URL" uses when web.playwright_enabled is on.
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "1")
    with rfp.remote_fetch_policy_scope({"local_only_mode": True}):
        rendered = br.browser_render("https://example.com/article")
    assert rendered["status"] == "egress_refused"
    assert windows_edge["launches"] == []


def test_windows_edge_argv_asks_for_no_component_update_or_sync(windows_edge: dict) -> None:
    argv, profile = br._chrome_argv(
        windows_edge["edge"], "https://example.com", user_agent="ua", virtual_time_ms=1000, screenshot_path=None
    )
    br._cleanup_isolated_profile(profile)
    assert "--disable-component-update" in argv
    assert "--disable-sync" in argv
    assert "--no-sandbox" not in argv


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_mac_and_linux_argv_is_unchanged(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(br.sys, "platform", platform)
    argv, profile = br._chrome_argv("/fake/chrome", "https://example.com", user_agent="ua", virtual_time_ms=1000, screenshot_path=None)
    br._cleanup_isolated_profile(profile)
    assert "--disable-component-update" not in argv
    assert "--disable-sync" not in argv
