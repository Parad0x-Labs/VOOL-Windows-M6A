"""VOOL must be quittable from its own window.

Clean-VM job 52 (port 76afbd36): closing the window leaves the bundle supervisor, the API (with
its broker) and Ollama running by design -- the next launch reopens the window fast -- but Windows
had no tray icon, no Quit item and no palette command, so a user could not end VOOL at all short
of Task Manager. The supervisor already stops everything it owns when ``<home>/run/stop`` appears
(``installer/bundle/bundle_supervisor.py`` run loop, the same file ``--stop`` and Stop_VOOL write).
Quit is that signal plus closing the window, reachable from the Home menu and the palette.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.runtime_paths as runtime_paths
from core.command_palette_fragment import render_palette_fragment
from core.vool_chat_page import render_vool_chat_html
from installer.bundle.vool_window import _WindowApi

_CATALOGS = sorted((Path(__file__).resolve().parents[1] / "core" / "i18n" / "catalogs").glob("*.json"))


class _Window:
    def __init__(self) -> None:
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


def _api(window: _Window) -> _WindowApi:
    api = _WindowApi()
    api.set_window(window)
    return api


def test_quit_signals_the_supervisor_and_closes_the_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_paths, "native_runtime_home", lambda *a, **k: tmp_path)
    window = _Window()

    result = _api(window).quit_app()

    assert result == {"ok": True}
    assert (tmp_path / "run" / "stop").read_text(encoding="utf-8") == "stop\n"
    assert window.destroyed == 1


def test_quit_that_cannot_signal_keeps_the_window_open(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "home"
    blocker.write_text("a file where the home folder should be", encoding="utf-8")
    monkeypatch.setattr(runtime_paths, "native_runtime_home", lambda *a, **k: blocker)
    window = _Window()

    result = _api(window).quit_app()

    assert result["ok"] is False and result["error"]
    # Closing the window without the signal would only hide VOOL again -- the exact bug.
    assert window.destroyed == 0


def test_palette_offers_quit_through_the_native_bridge() -> None:
    fragment = render_palette_fragment()
    assert "register('quit'," in fragment
    quit_line = next(line for line in fragment.splitlines() if "register('quit'," in line)
    assert "quitApp" in quit_line


def test_home_menu_has_a_quit_button_wired_to_the_native_bridge() -> None:
    page = render_vool_chat_html(build_commit="test-commit")
    assert 'id="quitBtn"' in page
    assert 'data-i18n="sidebar.quit"' in page
    assert "window.pywebview.api.quit_app" in page


@pytest.mark.parametrize("catalog", _CATALOGS, ids=[c.stem for c in _CATALOGS])
def test_every_locale_names_the_quit_button(catalog: Path) -> None:
    messages = json.loads(catalog.read_text(encoding="utf-8"))["messages"]
    assert messages.get("sidebar.quit", "").strip()
    assert messages.get("sidebar.quit_title", "").strip()
