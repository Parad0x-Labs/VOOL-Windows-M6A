"""The folder dialog opened from the Setup (or Settings) window belongs to THAT window.

Owner hand test, Windows 11, fresh install: in first-run Setup, "Choose a folder…" opened the OS
folder dialog and the Setup window seemed to close; after "Select folder" nothing moved on. The
bridge's ``pick_folder`` always opened the dialog on the CHAT window (``self._window``), so Windows
made the chat window the dialog's owner and raised it over Setup. Setup was still there, behind.

pywebview's js_api does not say which window called, so the page names its own window ("setup",
"settings") and the bridge maps that word to a window it already holds. The word picks one of
VOOL's own windows; it is never a path, and anything else falls back to the chat window exactly as
before. No new bridge method: the bridge's public surface is pinned elsewhere and stays as it is.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class _Window:
    def __init__(self, name: str, picked=None) -> None:
        self.name = name
        self.picked = picked
        self.dialogs: list = []
        self.shown = 0
        self.restored = 0

    def create_file_dialog(self, *args, **_kwargs):
        self.dialogs.append(args)
        return [self.picked] if self.picked else None

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        self.restored += 1


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(FOLDER_DIALOG=20))
    from installer.bundle.vool_window import _WindowApi

    bridge = _WindowApi()
    bridge.set_window(_Window("chat", "C:\\Users\\me\\chat-pick"))
    return bridge


def test_setup_asks_and_the_dialog_opens_on_the_setup_window(api) -> None:
    setup = _Window("setup", "C:\\Users\\me\\Projects")
    api._setup_window = setup
    assert api.pick_folder("setup") == {"ok": True, "path": "C:\\Users\\me\\Projects"}
    assert len(setup.dialogs) == 1, "the dialog must be owned by the Setup window"
    assert api._window.dialogs == [], "the chat window must not own (and so be raised by) Setup's dialog"
    assert setup.shown >= 1, "Setup comes back to the front after the dialog"


def test_a_cancelled_setup_dialog_still_brings_setup_back(api) -> None:
    setup = _Window("setup", None)
    api._setup_window = setup
    assert api.pick_folder("setup") == {"ok": False, "cancelled": True}
    assert len(setup.dialogs) == 1 and api._window.dialogs == []
    assert setup.shown >= 1


def test_the_chat_page_keeps_the_chat_window_as_owner(api) -> None:
    api._setup_window = _Window("setup", "C:\\elsewhere")
    assert api.pick_folder() == {"ok": True, "path": "C:\\Users\\me\\chat-pick"}
    assert len(api._window.dialogs) == 1 and api._setup_window.dialogs == []


def test_setup_without_its_window_falls_back_to_the_chat_window(api) -> None:
    assert api._setup_window is None
    assert api.pick_folder("setup") == {"ok": True, "path": "C:\\Users\\me\\chat-pick"}
    assert len(api._window.dialogs) == 1


@pytest.mark.parametrize("owner", ["C:\\Windows", "../../etc", "companion", 7, {"window": "setup"}])
def test_any_other_owner_word_is_the_chat_window_and_never_a_path(api, owner) -> None:
    api._setup_window = _Window("setup", "C:\\elsewhere")
    api._settings_window = _Window("settings", "C:\\elsewhere")
    assert api.pick_folder(owner) == {"ok": True, "path": "C:\\Users\\me\\chat-pick"}
    assert len(api._window.dialogs) == 1
    assert api._setup_window.dialogs == [] and api._settings_window.dialogs == []


def test_marking_from_settings_opens_the_picker_on_the_settings_window(api) -> None:
    settings = _Window("settings", None)          # cancelled: nothing reaches the store
    api._settings_window = settings
    assert api.mark_disposable_folder("settings") == {"ok": False, "cancelled": True}
    assert len(settings.dialogs) == 1 and api._window.dialogs == []
    assert settings.shown >= 1


def test_marking_with_a_page_supplied_path_still_uses_the_chat_window(api) -> None:
    api._window.picked = None
    api._settings_window = _Window("settings", None)
    assert api.mark_disposable_folder("C:\\some\\page\\path") == {"ok": False, "cancelled": True}
    assert len(api._window.dialogs) == 1 and api._settings_window.dialogs == []


def test_the_bridge_gains_no_new_method() -> None:
    from installer.bundle.vool_window import _WindowApi

    exposed = {name for name in dir(_WindowApi()) if not name.startswith("_")}
    assert not {name for name in exposed if "folder" in name} - {
        "pick_folder", "mark_disposable_folder", "unmark_disposable_folder"}


def test_the_setup_page_names_its_own_window_when_it_asks() -> None:
    from core.vool_setup_page import render_vool_setup_html

    html = render_vool_setup_html()
    picker = html.split("function pickFolder()", 1)[1].split("\n}\n", 1)[0]
    assert "window.pywebview.api.pick_folder('setup')" in picker, picker
    assert "api.pick_folder()" not in picker


def test_the_settings_page_names_its_own_window_when_it_marks() -> None:
    text = (ROOT / "core" / "vool_settings_page.py").read_text(encoding="utf-8")
    assert "window.pywebview.api.mark_disposable_folder('settings')" in text
    assert "window.pywebview.api.mark_disposable_folder()" not in text
