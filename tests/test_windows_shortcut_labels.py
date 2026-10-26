"""Windows: the shortcut key caps say Ctrl, the key a Windows keyboard has.

Every shortcut already answered Ctrl as well as ⌘, but the New chat button's key cap, the shortcut line under the
composer and the command palette's rows all said ⌘. A page served on Windows now says Ctrl+ for each of them; a Mac is
served exactly what it was.
"""
from __future__ import annotations

import sys

#: Every shortcut the palette fragment labels, and the two the companion drawer labels.
PALETTE_LABELS = ("Ctrl+K", "Ctrl+N", "Ctrl+J", "Ctrl+M", "Ctrl+/", "Ctrl+F", "Ctrl+,")
DRAWER_LABELS = ("Ctrl+N", "Ctrl+/")
MAC_MARKS = ("⌘", "\\u2318")


def _as_host(monkeypatch, host_platform: str, render):
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", host_platform)
        return render()


def test_the_windows_palette_says_ctrl_for_every_shortcut(monkeypatch) -> None:
    from core.command_palette_fragment import render_palette_fragment

    windows = _as_host(monkeypatch, "win32", render_palette_fragment)
    script = windows.split("<script>", 1)[1]
    assert not [mark for mark in MAC_MARKS if mark in script]
    for label in ("Ctrl+K", "Ctrl+N", "Ctrl+J", "Ctrl+/", "Ctrl+F", "Ctrl+,"):
        assert f"<kbd>{label}</kbd>" in script, label
    for label in ("Ctrl+N", "Ctrl+J", "Ctrl+M", "Ctrl+,"):
        assert f"}}, '{label}');" in script, label
    assert all(label in script for label in PALETTE_LABELS)


def test_the_windows_new_chat_key_cap_and_pet_row_say_ctrl(monkeypatch) -> None:
    from core.companion_drawer_fragment import render_companion_drawer_fragment

    windows = _as_host(monkeypatch, "win32", render_companion_drawer_fragment)
    assert not [mark for mark in MAC_MARKS if mark in windows]
    assert "chip.textContent = 'Ctrl+N';" in windows
    assert "toggleDrawer, 'Ctrl+/');" in windows
    assert all(label in windows for label in DRAWER_LABELS)


def test_a_mac_is_served_the_same_shortcut_labels(monkeypatch) -> None:
    from core.command_palette_fragment import _PALETTE_CSS, _PALETTE_JS, render_palette_fragment
    from core.companion_drawer_fragment import _DRAWER_CSS, _DRAWER_JS, render_companion_drawer_fragment

    assert _as_host(monkeypatch, "darwin", render_palette_fragment) == (
        "<style>" + _PALETTE_CSS + "</style><script>" + _PALETTE_JS + "</script>"
    )
    assert _as_host(monkeypatch, "darwin", render_companion_drawer_fragment) == (
        "<style>" + _DRAWER_CSS + "</style>\n<script>" + _DRAWER_JS + "</script>"
    )


def test_each_windows_label_replaces_exactly_one_mac_label() -> None:
    from core.command_palette_fragment import _PALETTE_JS
    from core.command_palette_fragment import _WINDOWS_SHORTCUT_LABELS as PALETTE_SWAPS
    from core.companion_drawer_fragment import _DRAWER_JS
    from core.companion_drawer_fragment import _WINDOWS_SHORTCUT_LABELS as DRAWER_SWAPS

    for script, swaps in ((_PALETTE_JS, PALETTE_SWAPS), (_DRAWER_JS, DRAWER_SWAPS)):
        for mac, windows in swaps:
            assert script.count(mac) == 1, mac
            assert windows not in script, windows
