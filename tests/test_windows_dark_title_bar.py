"""On Windows, VOOL's main, Settings and Setup windows get a dark title bar; the pet does not.

Owner hand test, Windows 11: the main window's title bar was plain white over VOOL's dark page.
The standard fix is DWM's immersive dark mode attribute on the window's HWND (attribute 20; 19 on
Windows 10 builds before 20H1), which also turns the minimise, maximise and close glyphs white,
plus DWMWA_CAPTION_COLOR (35) on Windows 11 for VOOL's own dark panel colour. The frameless pet has
no title bar and keeps its own window policy (pet_native_windows), so it is never touched.

Everything here is host-independent: the DWM calls go to a recording stand-in.
"""
from __future__ import annotations

import importlib
import inspect
import sys
import types

import pytest

from installer.bundle import windows_chrome as wc


class _Dwm:
    def __init__(self, results=None, raises=False) -> None:
        self.calls: list[tuple[int, int, int, int]] = []
        self.results = dict(results or {})
        self.raises = raises

    def DwmSetWindowAttribute(self, hwnd, attribute, value_ref, size):
        if self.raises:
            raise OSError("dwmapi is not there")
        value = value_ref._obj.value if hasattr(value_ref, "_obj") else int(value_ref)
        self.calls.append((int(hwnd), int(attribute), int(value), int(size)))
        return self.results.get(int(attribute), 0)


def test_the_attribute_ids_are_the_documented_ones() -> None:
    assert wc.DWMWA_USE_IMMERSIVE_DARK_MODE == 20
    assert wc.DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 == 19
    assert wc.DWMWA_CAPTION_COLOR == 35


def test_the_caption_colour_is_the_dark_panel_as_a_colorref() -> None:
    from core.ui_palette import DARK_PALETTE_CSS

    assert "--panel:#1b1b1e" in DARK_PALETTE_CSS.replace(" ", "")
    assert wc.CAPTION_COLOR_HEX == "#1b1b1e"
    assert wc.colorref("#1b1b1e") == 0x001E1B1B      # 0x00BBGGRR
    assert wc.colorref("#102030") == 0x00302010


def test_dark_mode_is_asked_with_true_as_a_four_byte_value() -> None:
    dwm = _Dwm()
    assert wc.apply_dark_title_bar(0x1234, dwmapi=dwm) is True
    assert dwm.calls[0] == (0x1234, 20, 1, 4)
    assert (0x1234, 35, 0x001E1B1B, 4) in dwm.calls
    assert all(call[1] != 19 for call in dwm.calls), "no fallback when 20 is accepted"


def test_older_windows_10_falls_back_to_attribute_19() -> None:
    dwm = _Dwm(results={20: -2147024809})       # E_INVALIDARG: build without attribute 20
    assert wc.apply_dark_title_bar(0x1234, dwmapi=dwm) is True
    assert [call[1] for call in dwm.calls][:2] == [20, 19]
    assert dwm.calls[1] == (0x1234, 19, 1, 4)


def test_a_caption_colour_refusal_does_not_undo_dark_mode() -> None:
    dwm = _Dwm(results={35: -2147024809})       # Windows 10: no caption colour attribute
    assert wc.apply_dark_title_bar(0x1234, dwmapi=dwm) is True


def test_every_failure_is_a_silent_no_op() -> None:
    assert wc.apply_dark_title_bar(0x1234, dwmapi=_Dwm(raises=True)) is False
    assert wc.apply_dark_title_bar(0x1234, dwmapi=_Dwm(results={20: -1, 19: -1})) is False
    assert wc.apply_dark_title_bar(0, dwmapi=_Dwm()) is False
    assert wc.apply_dark_title_bar(None, dwmapi=_Dwm()) is False


def test_the_gate_is_windows_only(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(wc, "apply_dark_title_bar", lambda hwnd, **_k: seen.append(hwnd) or True)
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x99)))
    for platform in ("darwin", "linux", "cygwin"):
        assert wc.style_native_window(window, platform=platform, background=False) is False
    assert seen == []
    assert wc.style_native_window(window, platform="win32", background=False) is True
    assert seen == [0x99]


def test_a_window_without_a_handle_is_a_no_op(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(wc, "apply_dark_title_bar", lambda hwnd, **_k: seen.append(hwnd) or True)
    assert wc.style_native_window(types.SimpleNamespace(), platform="win32", background=False, wait=0.0) is True
    assert seen == []


def test_styling_never_raises_into_the_caller(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("DWM went away")

    monkeypatch.setattr(wc, "apply_dark_title_bar", boom)
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x99)))
    assert wc.style_native_window(window, platform="win32", background=False) is True


def test_off_windows_the_real_call_is_a_no_op() -> None:
    if sys.platform == "win32":
        pytest.skip("off-Windows row")
    assert wc.apply_dark_title_bar(0x1234) is False


# --- which windows ------------------------------------------------------------------------------


class _Created:
    def __init__(self) -> None:
        self.events = types.SimpleNamespace(closed=_Hook())


class _Hook:
    def __iadd__(self, _handler):
        return self


@pytest.fixture
def styled(monkeypatch):
    from installer.bundle import vool_window as nw

    seen: list = []
    monkeypatch.setattr(wc, "style_native_window", lambda window, **_k: seen.append(window) or True)
    return nw, seen


def test_settings_and_setup_windows_are_styled(styled) -> None:
    nw, seen = styled
    api = nw._WindowApi()
    made: list = []
    webview = types.SimpleNamespace(create_window=lambda *a, **k: made.append(_Created()) or made[-1])
    api.set_window(object(), webview)
    assert api.open_settings()["ok"] is True
    assert api.open_setup()["ok"] is True
    assert seen == made and len(made) == 2


def test_the_main_window_is_styled() -> None:
    from installer.bundle import vool_window as nw

    # Opened through the one helper that styles every VOOL window (see
    # tests/test_every_vool_window_gets_windows_chrome.py), before the GUI loop starts.
    main = inspect.getsource(nw.main)
    helper = inspect.getsource(nw._create_vool_window)
    assert "windows_chrome.style_native_window(" in helper
    assert main.index('_create_vool_window(\n            webview, "VOOL", URL') < main.index("webview.start(")


def test_the_frameless_pet_is_never_styled() -> None:
    from installer.bundle import vool_window as nw

    assert "windows_chrome" not in inspect.getsource(nw._WindowApi.detach_companion)
    assert nw.COMPANION_WINDOW_FLAGS["frameless"] is True
    pet = importlib.import_module("installer.bundle.pet_native_windows")
    assert "windows_chrome" not in inspect.getsource(pet)
