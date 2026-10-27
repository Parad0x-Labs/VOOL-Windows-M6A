"""Windows: the catalog texts that name a Mac say what Windows has, in every shipped language.

First-run setup offered "On my Mac", the folder step's example was a Mac path, the chat filter's hint was ⌘F, and
Settings described macOS notifications and Apple Calendar "on this Mac". Each of those keys now has a ``.windows``
sibling in all 17 catalogs, and a page served on Windows shows the sibling. A Mac is served exactly what it was.
"""
from __future__ import annotations

import json
import sys

import pytest

from core.i18n.catalog import CATALOGS_DIR
from core.i18n.host_text import host_key
from tests.chat_page_js_harness import run_node, slice_source

#: Catalog keys whose English names a Mac; each has a ``<key>.windows`` sibling.
MAC_KEYS = (
    "setup.caption.thinking",
    "setup.status.saved_thinking",
    "setup.step.name.sentence",
    "setup.step.thinking.sentence",
    "setup.thinking.local_title",
    "setup.thinking.online_sub",
    "setup.folder.placeholder",
    "chats.filter_placeholder",
    "settings.group.notifications.blurb",
    "settings.row.notifications_panel.label",
    "settings.row.notifications_panel.help",
    "settings.row.calendar_accounts_panel.help",
)
#: The keys the setup page's own script asks for (the others reach it through the step model and captions).
SETUP_SCRIPT_KEYS = (
    "setup.thinking.local_title",
    "setup.thinking.online_sub",
    "setup.status.saved_thinking",
    "setup.folder.placeholder",
)
LOCALES = sorted(path.stem for path in CATALOGS_DIR.glob("*.json"))


def _messages(tag: str) -> dict[str, str]:
    return json.loads((CATALOGS_DIR / f"{tag}.json").read_text(encoding="utf-8"))["messages"]


def _as_host(monkeypatch, host_platform: str, render):
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", host_platform)
        return render()


def _shown(tag: str) -> dict[str, str]:
    """What setup and Settings show for each key that can name a Mac, in ``tag``, on the current host."""
    from core.vool_settings_page import _localized_settings_groups
    from core.vool_setup_page import _localized_captions, setup_steps_model

    steps = {step["id"]: step for step in setup_steps_model(tag)}
    groups = {group["id"]: group for group in _localized_settings_groups(tag)}
    rows = {row["id"]: row for group in groups.values() for row in group.get("rows", [])}
    return {
        "setup.caption.thinking": _localized_captions(tag)["thinking"],
        "setup.step.name.sentence": steps["name"]["sentence"],
        "setup.step.thinking.sentence": steps["thinking"]["sentence"],
        "settings.group.notifications.blurb": groups["notifications"]["blurb"],
        "settings.row.notifications_panel.label": rows["notifications_panel"]["label"],
        "settings.row.notifications_panel.help": rows["notifications_panel"]["help"],
        "settings.row.calendar_accounts_panel.help": rows["calendar_accounts_panel"]["help"],
    }


@pytest.mark.parametrize("tag", LOCALES)
def test_every_catalog_carries_a_windows_text_for_each_mac_text(tag) -> None:
    messages = _messages(tag)
    missing = [f"{key}.windows" for key in MAC_KEYS if f"{key}.windows" not in messages]
    assert not missing, f"{tag}: no Windows text for {missing}"
    same = [key for key in MAC_KEYS if messages[f"{key}.windows"] == messages[key]]
    assert not same, f"{tag}: the Windows text repeats the Mac text for {same}"
    still_mac = [key for key in MAC_KEYS if "Mac" in messages[f"{key}.windows"] or "⌘" in messages[f"{key}.windows"]]
    assert not still_mac, f"{tag}: a Windows text still names a Mac: {still_mac}"


@pytest.mark.parametrize("tag", LOCALES)
def test_windows_setup_and_settings_show_the_windows_text(monkeypatch, tag) -> None:
    messages = _messages(tag)
    shown = _as_host(monkeypatch, "win32", lambda: _shown(tag))
    for key, text in shown.items():
        assert text == messages[f"{key}.windows"], f"{tag}: Windows shows {text!r} for {key}"
        assert text != messages[key]


@pytest.mark.parametrize("tag", LOCALES)
def test_a_mac_still_shows_the_mac_text(monkeypatch, tag) -> None:
    messages = _messages(tag)
    shown = _as_host(monkeypatch, "darwin", lambda: _shown(tag))
    for key, text in shown.items():
        assert text == messages[key], f"{tag}: a Mac shows {text!r} for {key}"


def test_the_windows_setup_script_asks_for_the_windows_keys(monkeypatch) -> None:
    from core.vool_setup_page import render_vool_setup_html

    windows = _as_host(monkeypatch, "win32", lambda: render_vool_setup_html(ui_locale="en"))
    mac = _as_host(monkeypatch, "darwin", lambda: render_vool_setup_html(ui_locale="en"))
    english = _messages("en")
    for key in SETUP_SCRIPT_KEYS:
        assert f"T('{key}'," not in windows
        assert f"T('{key}.windows', '{english[key + '.windows']}')".replace("\\", "\\\\") in windows
        assert f"T('{key}'," in mac and f"T('{key}.windows'," not in mac
    assert "On my Mac" not in windows.split('<script id="vool-i18n">')[0]


def test_the_windows_chat_filter_hint_says_ctrl(monkeypatch) -> None:
    from core.composer_extras_fragment import render_composer_extras_fragment

    windows = _as_host(monkeypatch, "win32", render_composer_extras_fragment)
    mac = _as_host(monkeypatch, "darwin", render_composer_extras_fragment)
    assert "extrasT('chats.filter_placeholder.windows', 'Search chats\\u2026  Ctrl+F')" in windows
    assert "\\u2318F" not in windows
    assert "extrasT('chats.filter_placeholder', 'Search chats\\u2026  \\u2318F')" in mac


def test_a_mac_page_is_served_as_before(monkeypatch) -> None:
    from core.composer_extras_fragment import _EXTRAS_CSS, _EXTRAS_JS, render_composer_extras_fragment
    from core.vool_settings_page import _localized_settings_groups, settings_groups
    from core.vool_setup_page import _SETUP_JS, _host_setup_script

    assert _as_host(monkeypatch, "darwin", _host_setup_script) == _SETUP_JS
    assert _as_host(monkeypatch, "darwin", render_composer_extras_fragment) == (
        "<style>" + _EXTRAS_CSS + "</style><script>" + _EXTRAS_JS + "</script>"
    )
    assert _as_host(monkeypatch, "darwin", lambda: _localized_settings_groups("en")) == settings_groups()


def test_host_key_picks_the_windows_sibling_only_on_windows_and_only_when_it_exists() -> None:
    assert host_key("setup.thinking.local_title", windows=True) == "setup.thinking.local_title.windows"
    assert host_key("setup.thinking.local_title", windows=False) == "setup.thinking.local_title"
    assert host_key("setup.thinking.local_sub", windows=True) == "setup.thinking.local_sub"


@pytest.mark.parametrize(
    ("typed", "name"),
    [
        ("C:\\Users\\you\\Documents", "Documents"),
        ("C:\\Users\\you\\Documents\\", "Documents"),
        ("/Users/you/Documents", "Documents"),
        ("/Users/you/Documents/", "Documents"),
        ("C:\\", "C:"),
    ],
)
def test_a_typed_folder_names_the_project_by_its_last_folder(typed, name) -> None:
    from core.vool_setup_page import _SETUP_JS

    line = slice_source(_SETUP_JS, "    const name = clean.split(", "\n")
    out = run_node(f"const clean = {json.dumps(typed)};\n{line}\nconsole.log(JSON.stringify({{name}}));\n")
    assert out["name"] == name
