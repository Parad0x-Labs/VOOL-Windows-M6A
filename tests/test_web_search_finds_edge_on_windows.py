"""On Windows the browser behind web search is the Edge installed in Program Files, and only that.

`tools/browser/browser_render.find_browser_binary` had no Windows branch. It read the Playwright
cache from `~/.cache`, then macOS app paths, then Linux command names on PATH, so every Windows
machine answered None. `browser_search` was then dropped from the search chain with no note, and
answers came from the scrapers that the search module itself says are served a decoy index.

It now asks the same lookup as the browser lane (`core.vool_browser.engine`): the Edge under the
Program Files folders the shell reports. PATH, a per-user install, a Chrome beside Edge and the
Playwright cache under the profile are never used. And when the chain asked for `browser_search`
and it cannot run, the research notes say so.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import policy_engine, windows_known_folders
from tools.browser import browser_render as br
from tools.web import web_research

_EDGE = "Microsoft\\Edge\\Application\\msedge.exe"


def _install(base: Path, relative: str) -> Path:
    exe = base.joinpath(*relative.split("\\"))
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    exe.chmod(0o755)
    return exe


@pytest.fixture()
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    monkeypatch.delenv("VOOL_BROWSER_BINARY", raising=False)
    monkeypatch.delenv("VOOL_BROWSER_ENGINE", raising=False)
    monkeypatch.setattr(br.sys, "platform", "win32")
    folders = {"x64": tmp_path / "Program Files", "x86": tmp_path / "Program Files (x86)"}
    for folder in folders.values():
        folder.mkdir()
    monkeypatch.setattr(windows_known_folders, "program_files_folders", lambda: [str(f) for f in folders.values()])
    # Everything the old lookup would have taken, planted where it looked:
    # a browser on PATH under Linux and Windows names ...
    on_path = tmp_path / "bin"
    for name in ("chromium", "google-chrome", "microsoft-edge", "msedge.exe", "chrome.exe"):
        _install(on_path, name)
    monkeypatch.setenv("PATH", str(on_path) + os.pathsep + os.environ.get("PATH", ""))
    # ... and a Playwright cache holding a Linux and a Windows Chromium build.
    cache = tmp_path / "ms-playwright"
    _install(cache, "chromium-1248\\chrome-linux\\chrome")
    _install(cache, "chromium-1248\\chrome-win\\chrome.exe")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    br.reset_browser_binary_cache_for_test()
    yield folders
    br.reset_browser_binary_cache_for_test()


def test_edge_in_program_files_x86_is_the_search_browser(windows: dict[str, Path]) -> None:
    exe = _install(windows["x86"], _EDGE)
    assert br.find_browser_binary() == str(exe)


def test_edge_in_program_files_is_the_search_browser(windows: dict[str, Path]) -> None:
    exe = _install(windows["x64"], _EDGE)
    assert br.find_browser_binary() == str(exe)


def test_edge_wins_over_chrome_in_another_program_files_folder(windows: dict[str, Path]) -> None:
    _install(windows["x64"], "Google\\Chrome\\Application\\chrome.exe")
    exe = _install(windows["x86"], _EDGE)
    assert br.find_browser_binary() == str(exe)


def test_without_edge_nothing_on_path_or_in_the_playwright_cache_is_used(windows: dict[str, Path]) -> None:
    _install(windows["x64"], "Google\\Chrome\\Application\\chrome.exe")
    assert br.find_browser_binary() is None


def test_the_search_lane_and_the_browser_lane_launch_the_same_edge(windows: dict[str, Path]) -> None:
    from core.vool_browser import engine

    _install(windows["x64"], _EDGE)
    assert br.find_browser_binary() == engine.find_engine_binary()


def test_an_explicit_pin_still_wins_on_windows(windows: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(windows["x64"], _EDGE)
    pinned = _install(tmp_path / "pin", "browser.exe")
    monkeypatch.setenv("VOOL_BROWSER_BINARY", str(pinned))
    assert br.find_browser_binary() == str(pinned)


def test_with_edge_present_browser_search_stays_in_the_chain(windows: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _install(windows["x86"], _EDGE)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_ORDER", raising=False)
    monkeypatch.setenv("ALLOW_BROWSER_FALLBACK", "1")
    assert "browser_search" in web_research._provider_order()
    assert web_research._browser_search_dropped_note() is None


def test_the_launcher_order_still_gets_browser_search_once_edge_is_found(windows: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    # Start_VOOL.bat sets this order, which predates browser_search; the policy-allowed engines are
    # appended, so the provider runs as soon as a browser is found.
    _install(windows["x86"], _EDGE)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_ORDER", "searxng,ddg_instant,duckduckgo_html")
    monkeypatch.setenv("ALLOW_BROWSER_FALLBACK", "1")
    if "browser_search" not in policy_engine.allowed_web_engines():
        pytest.skip("this policy does not allow browser_search")
    assert "browser_search" in web_research._provider_order()


def test_a_missing_browser_is_named_in_the_research_notes(windows: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_ORDER", raising=False)
    monkeypatch.setenv("ALLOW_BROWSER_FALLBACK", "1")
    assert "browser_search" not in web_research._provider_order()
    assert web_research._browser_search_dropped_note() == "browser_search_skipped:no_browser"

    # The note reaches the result of a real research call, offline: every provider left in the
    # chain answers empty, and nothing opens a socket.
    monkeypatch.setattr(web_research, "remote_fetch_forbidden", lambda: False)
    monkeypatch.setattr(web_research, "_prefer_specialized_live_research", lambda _q: False)
    monkeypatch.setattr(web_research, "_provider_order", lambda: [])
    result = web_research.web_research("latest news about jupiter", total_budget_s=5.0)
    assert "browser_search_skipped:no_browser" in result.notes


def test_a_browser_switched_off_by_policy_is_named_as_such(windows: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _install(windows["x86"], _EDGE)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_ORDER", raising=False)
    monkeypatch.setenv("ALLOW_BROWSER_FALLBACK", "0")
    assert web_research._browser_search_dropped_note() == "browser_search_skipped:disabled_by_policy"


def test_a_chain_that_never_asked_for_browser_search_gets_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_research, "_configured_provider_order", lambda: ["searxng", "ddg_instant"])
    assert web_research._browser_search_dropped_note() is None
