"""A chat bundle exported from Settings saves through a dialog that belongs to Settings, and the
result line stays on screen.

Steve round 16 on 9b30ae95: Settings > Backup > Export chat opened its Save As dialog owned by the
CHAT window, so Windows raised the chat over Settings. pywebview's js_api does not say which window
called, so the Settings page names its own window ("settings"), as Setup and Settings already do for
the folder pickers (``_WindowApi._dialog_owner``). After the save the form also reset with no result
line: closing the dialog gives focus back to Settings, and the focus handler rebuilds the pane, which
built a fresh, empty export widget. The result line and the chosen chat now outlive the rebuild.
"""
from __future__ import annotations

import pathlib
import re
import types

import pytest

from core.vool_settings_page import render_vool_settings_html
from installer.bundle import vool_window
from installer.bundle.vool_window import _WindowApi

_BUNDLE_PATH = "/home/u/.vool/session_bundles/chat-0123456789abcdef-20261111T000000Z.voolsession"


class _Headers:
    def get_content_type(self) -> str:
        return "application/octet-stream"

    def get(self, name: str, default: str = "") -> str:
        return default


class _Response:
    headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return b"VOOLSESSION\x00signed bytes"


class _Webview:
    SAVE_DIALOG = 1


class _Window:
    def __init__(self, title: str, destination: pathlib.Path | None) -> None:
        self.title = title
        self._destination = destination
        self.dialogs = 0
        self.shown = 0
        self.restored = 0

    def create_file_dialog(self, *args: object, **kwargs: object):
        self.dialogs += 1
        return (str(self._destination),) if self._destination is not None else None

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        self.restored += 1


@pytest.fixture
def windows(tmp_path, monkeypatch):
    opener = types.SimpleNamespace(open=lambda url, timeout=0: _Response())
    monkeypatch.setattr(vool_window.urllib.request, "build_opener", lambda *a, **k: opener)
    chat = _Window("VOOL", tmp_path / "from-chat.voolsession")
    settings = _Window("VOOL Settings", tmp_path / "from-settings.voolsession")
    api = _WindowApi()
    api.set_window(chat, _Webview())
    return api, chat, settings, tmp_path


def test_an_export_from_settings_opens_its_dialog_on_settings(windows) -> None:
    api, chat, settings, tmp_path = windows
    api._settings_window = settings
    result = api.save_session_bundle(_BUNDLE_PATH, "s", "settings")
    assert result["ok"] is True
    assert (settings.dialogs, chat.dialogs) == (1, 0)
    assert (tmp_path / "from-settings.voolsession").exists()
    # Settings comes back to the front after the dialog, the way the folder pickers do.
    assert settings.shown >= 1
    assert chat.shown == 0 and chat.restored == 0


def test_a_cancelled_settings_save_still_brings_settings_back(windows) -> None:
    api, chat, settings, _tmp = windows
    settings._destination = None
    api._settings_window = settings
    assert api.save_session_bundle(_BUNDLE_PATH, "s", "settings") == {"ok": False, "cancelled": True}
    assert settings.dialogs == 1 and settings.shown >= 1 and chat.dialogs == 0


def test_the_chat_page_export_keeps_the_chat_window(windows) -> None:
    api, chat, settings, tmp_path = windows
    api._settings_window = settings
    assert api.save_session_bundle(_BUNDLE_PATH, "s")["ok"] is True
    assert (chat.dialogs, settings.dialogs) == (1, 0)
    assert chat.shown == 0, "a chat-owned dialog does not re-raise the chat window"


def test_settings_named_but_closed_falls_back_to_the_chat_window(windows) -> None:
    api, chat, _settings, _tmp = windows
    assert api.save_session_bundle(_BUNDLE_PATH, "s", "settings")["ok"] is True
    assert chat.dialogs == 1


@pytest.mark.parametrize("owner", ["C:\\\\Windows", "../settings", {"window": "settings"}, 7])
def test_an_owner_is_only_ever_a_window_word(windows, owner) -> None:
    api, chat, settings, _tmp = windows
    api._settings_window = settings
    assert api.save_session_bundle(_BUNDLE_PATH, "s", owner)["ok"] is True
    assert (chat.dialogs, settings.dialogs) == (1, 0)


def _export_handler() -> str:
    html = render_vool_settings_html()
    start = html.index("function widgetBundleExport(stack)")
    end = html.index("function widgetBundleImport(stack)")
    return html[html.rindex("const bundleExportMemory", 0, start):end]


def test_the_settings_page_names_its_own_window() -> None:
    handler = _export_handler()
    assert re.search(r"save_session_bundle\([^;]*,\s*'settings'\)", handler), "Settings must name itself as the owner"


def test_the_result_line_and_chosen_chat_outlive_a_pane_rebuild() -> None:
    handler = _export_handler()
    # Kept outside the widget, at page scope, so a rebuilt widget can show them again.
    assert re.search(r"const bundleExportMemory = \{ sid: '', note: null \};\s*function widgetBundleExport", handler)
    assert "if (bundleExportMemory.note) paint(status, bundleExportMemory.note);" in handler
    assert "chatSel.value = bundleExportMemory.sid" in handler
    # A save that finishes after the rebuild reports into the widget that is on screen now.
    assert "document.getElementById('sbExportStatus')" in handler
    # Every message goes through the kept note; nothing writes the status element directly.
    assert "status.textContent" not in handler
    # Never a passphrase.
    memory_uses = re.findall(r"bundleExportMemory\.(\w+)", handler)
    assert set(memory_uses) <= {"sid", "note"}, memory_uses


def test_settings_still_rebuilds_its_pane_on_focus() -> None:
    """The fix keeps the rebuild (it picks up changes made elsewhere); it only stops losing state."""
    assert "window.addEventListener('focus', () => { reconcile(); });" in render_vool_settings_html()


def test_the_saved_bundle_reports_where_it_went(windows) -> None:
    api, _chat, settings, tmp_path = windows
    api._settings_window = settings
    result = api.save_session_bundle(_BUNDLE_PATH, "s", "settings")
    assert result == {"ok": True, "path": str(tmp_path / "from-settings.voolsession")}


@pytest.mark.parametrize("page", ["settings", "chat"])
def test_both_export_lines_name_the_saved_path(page) -> None:
    from core.vool_chat_page import render_vool_chat_html

    html = render_vool_settings_html() if page == "settings" else render_vool_chat_html()
    marker = "exportBtn.addEventListener('click'" if page == "settings" else "sbExportBtn.addEventListener('click'"
    handler = html[html.index(marker):html.index(marker) + 5000]
    assert "let where = '';" in handler
    assert "if (typeof saved.path === 'string' && saved.path) where = ' to ' + saved.path;" in handler
    assert "' turns)' + where + '.'" in handler
    # The path is shown as text, never as markup.
    saved_line = handler[handler.index("'Saved: signed"):]
    assert "innerHTML" not in saved_line[:400]
