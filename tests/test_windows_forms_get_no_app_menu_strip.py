"""On Windows no VOOL form gets an application MenuStrip; macOS keeps its Settings menu item.

The window host passes ``menu=[Menu("__app__", [Settings...])]`` to ``webview.start``. On macOS
pywebview merges a menu titled ``__app__`` into the application menu (where Settings belongs). On
Windows, pywebview's WinForms backend instead gives EVERY form a ``MenuStrip`` built from the start
menu (``if window.menu or _state['menu']: self.set_window_menu(...)``): a white band under the title
bar of the main, Settings and Setup windows (owner screenshot), and a white strip across the top
of the frameless pet, which shows through the pet's top-right controls island. Windows already
reaches Settings from the page, so Windows starts with no app menu at all.

Host-independent: the menu API is a stand-in module and the platform is set per row.
"""
from __future__ import annotations

import inspect
import sys
import types

import pytest


class _Menu:
    def __init__(self, title, items=None) -> None:
        self.title, self.items = title, list(items or [])


class _MenuAction:
    def __init__(self, title, function) -> None:
        self.title, self.function = title, function


@pytest.fixture
def nw(monkeypatch):
    from installer.bundle import vool_window

    menu = types.ModuleType("webview.menu")
    menu.Menu, menu.MenuAction = _Menu, _MenuAction
    monkeypatch.setitem(sys.modules, "webview.menu", menu)
    return vool_window


def test_windows_starts_with_no_app_menu(nw, monkeypatch) -> None:
    monkeypatch.setattr(nw.sys, "platform", "win32")
    assert nw._settings_menu(object()) == []


def test_macos_keeps_the_settings_item_in_the_app_menu(nw, monkeypatch) -> None:
    monkeypatch.setattr(nw.sys, "platform", "darwin")
    menus = nw._settings_menu(object())
    assert [m.title for m in menus] == ["__app__"]
    assert [item.title for item in menus[0].items] == ["Settings…"]


def test_main_hands_webview_start_exactly_that_menu() -> None:
    from installer.bundle import vool_window

    main = inspect.getsource(vool_window.main)
    assert "menu = _settings_menu(api)" in main
    assert "webview.start(_post_start_all, menu=menu, **start_options)" in main
