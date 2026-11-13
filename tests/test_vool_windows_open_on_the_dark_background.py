"""Every VOOL window opens on VOOL's dark background, so there is no white flash before the page.

pywebview paints a new window #FFFFFF until the page draws (WebView2's default background), so the
chat, Settings and Setup windows flashed white each time they opened. The one helper that opens a
VOOL window gives it ``background_color`` = the UI's ``--bg``. The transparent pet keeps its own
flags (black at alpha 0 under ``transparent=True``) and never goes through the helper.
"""
from __future__ import annotations

import types


def _module():
    from installer.bundle import vool_window

    return vool_window


def test_the_background_is_the_pages_own_bg() -> None:
    from core.ui_palette import DARK_PALETTE_CSS

    nw = _module()
    assert nw.WINDOW_BACKGROUND == "#141416"
    assert "--bg:#141416" in DARK_PALETTE_CSS.replace(" ", "")


def test_the_helper_opens_every_window_on_it(monkeypatch) -> None:
    nw = _module()
    from installer.bundle import windows_chrome

    monkeypatch.setattr(windows_chrome, "style_native_window", lambda *_a, **_k: True)
    seen: list = []
    module = types.SimpleNamespace(create_window=lambda *a, **k: seen.append(k) or "W")
    nw._create_vool_window(module, "VOOL Anything", "http://x", width=10)
    assert seen[-1]["background_color"] == "#141416"
    assert seen[-1]["width"] == 10


def test_an_explicit_background_is_kept(monkeypatch) -> None:
    nw = _module()
    from installer.bundle import windows_chrome

    monkeypatch.setattr(windows_chrome, "style_native_window", lambda *_a, **_k: True)
    seen: list = []
    module = types.SimpleNamespace(create_window=lambda *a, **k: seen.append(k) or "W")
    nw._create_vool_window(module, "VOOL Anything", background_color="#202020")
    assert seen[-1]["background_color"] == "#202020"


def test_settings_and_setup_open_on_it(monkeypatch) -> None:
    nw = _module()
    from installer.bundle import windows_chrome

    monkeypatch.setattr(windows_chrome, "style_native_window", lambda *_a, **_k: True)
    seen: list = []

    class _Hook:
        def __iadd__(self, _handler):
            return self

    def create(*_a, **k):
        seen.append(k)
        return types.SimpleNamespace(events=types.SimpleNamespace(closed=_Hook()))

    api = nw._WindowApi()
    api.set_window(object(), types.SimpleNamespace(create_window=create))
    assert api.open_settings()["ok"] and api.open_setup()["ok"]
    assert [k["background_color"] for k in seen] == ["#141416", "#141416"]


def test_the_transparent_pet_keeps_its_own_flags() -> None:
    nw = _module()
    assert nw.COMPANION_WINDOW_FLAGS["transparent"] is True
    assert nw.COMPANION_WINDOW_FLAGS["background_color"] == "#000000"
