"""The detached pet on Windows gets the same desktop rules as the Mac pet.

Parity gap (7 Nov 2026 audit of the port against Mac v0.7.1-beta): ``pet_native`` applies the pet's
desktop behaviour through AppKit only. On Windows the controller found no NSWindow, so the pet's
WinForms/WebView2 window kept none of it: a taskbar button, not above other windows, its empty
margin catching clicks, no saved position, no drop at the pointer. Measured on Windows 11
(probe, 7 Nov): ex-style 0x08050000 (APPWINDOW, not TOPMOST), a solid grey box round the character,
the pet taking the keyboard from the window in front, and the window came up 192x197 for a
208x236 pet because pywebview sizes the form before it drops the frame.

These drive the pure geometry and the Win32 controller/tracker through a fake user32 seam, so
they run on every platform; the real window is proven on Windows by the probe.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from installer.bundle import pet_native
from installer.bundle import pet_native_windows as pw

W, H = float(pet_native.PET_WINDOW_WIDTH), float(pet_native.PET_WINDOW_HEIGHT)


# --------------------------------------------------------------------- pure geometry


def _canvas_centre(frame, scale=1.0):
    """Win32 screen point at the character's centre (top-left origin, y down)."""
    fx, fy, fw, fh = frame
    centre_from_bottom = (pet_native.CANVAS_BOTTOM + pet_native.PET_CANVAS_POINTS / 2.0) * scale
    return (fx + fw / 2.0, fy + fh - centre_from_bottom)


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_the_character_takes_the_click_and_the_empty_corner_does_not(scale):
    frame = (300.0, 200.0, W * scale, H * scale)
    cx, cy = _canvas_centre(frame, scale)
    assert pw.point_hits_pet(cx, cy, frame) is True
    # The top-left corner of the window is empty margin: not part of the pet.
    assert pw.point_hits_pet(frame[0] + 2 * scale, frame[1] + 2 * scale, frame) is False
    # The bottom-left corner beside the canvas is empty too.
    assert pw.point_hits_pet(frame[0] + 2 * scale, frame[1] + frame[3] - 2 * scale, frame) is False


def test_the_controls_sit_top_right_in_windows_orientation():
    """Shared hit rects are bottom-left origin; the controls must land at the window's TOP."""
    frame = (0.0, 0.0, W, H)
    top_right = (W - 10.0, 14.0)            # right of the canvas, inside the control strip
    bottom_right = (W - 10.0, H - 14.0)     # the same spot measured from the bottom edge
    assert pw.point_hits_pet(*top_right, frame) is True
    assert pw.point_hits_pet(*bottom_right, frame) is False


def test_the_yield_region_hands_the_click_to_the_surface_underneath():
    frame = (100.0, 100.0, W, H)
    cx, cy = _canvas_centre(frame)
    assert pw.point_hits_pet(cx, cy, frame, avoid_rect=(cx - 5, cy - 5, 10, 10)) is False
    assert pw.point_hits_pet(cx, cy, frame, avoid_rect=(0, 0, 10, 10)) is True


def test_a_first_pet_goes_lower_right_of_the_primary_display():
    # The primary display holds the origin even when it is listed second; the other is to its left.
    areas = [(-1920.0, 0.0, 1920.0, 1040.0), (0.0, 0.0, 2560.0, 1400.0)]
    frame = pw.default_pet_frame(areas, (W, H))
    assert frame == (2560.0 - W - 48.0, 1400.0 - H - 96.0, W, H)
    scaled = pw.default_pet_frame([(0.0, 0.0, 2880.0, 1752.0)], (W * 1.5, H * 1.5))
    assert scaled == (2880.0 - W * 1.5 - 72.0, 1752.0 - H * 1.5 - 144.0, W * 1.5, H * 1.5)


def test_a_saved_spot_on_an_unplugged_monitor_comes_back_onto_a_display():
    areas = [(0.0, 0.0, 1920.0, 1040.0)]
    frame, restored = pw.placed_frame({"x": 4000.0, "y": 300.0}, areas, (W, H))
    assert restored is True
    assert 0.0 <= frame[0] <= 1920.0 - W and 0.0 <= frame[1] <= 1040.0 - H
    kept, restored = pw.placed_frame({"x": 500.0, "y": 400.0}, areas, (W, H))
    assert kept == (500.0, 400.0, W, H) and restored is True
    fallback, restored = pw.placed_frame({"x": "nope"}, areas, (W, H))
    assert restored is False and fallback == pw.default_pet_frame(areas, (W, H))


def test_page_rects_scale_to_pixels_and_bad_ones_are_refused():
    assert pw.page_rect_to_screen({"x": 10, "y": 20, "width": 300, "height": 80}, 1.5) == (15.0, 30.0, 450.0, 120.0)
    for bad in (None, {"x": "a", "y": 1, "width": 1, "height": 1}, {"x": 1, "y": 1, "width": 0, "height": 1},
                {"x": float("nan"), "y": 1, "width": 1, "height": 1}, {"x": 0, "y": 0, "width": 30000, "height": 1}):
        assert pw.page_rect_to_screen(bad, 1.0) is None


def test_the_window_style_policy():
    shipped = 0x08050000                      # what pywebview gave the pet (measured)
    style = pw.pet_ex_style(shipped)
    assert style & pw.WS_EX_NOACTIVATE        # never takes the keyboard
    assert style & pw.WS_EX_TOOLWINDOW        # no taskbar button, no Alt+Tab entry
    assert not style & pw.WS_EX_APPWINDOW
    # A layered pet hit-tests by its own surface, which WebView2 does not draw into: measured,
    # a colour-keyed pet let the click through the character itself. Never layered.
    assert not pw.pet_ex_style(shipped | pw.WS_EX_LAYERED | pw.WS_EX_TRANSPARENT) & (
        pw.WS_EX_LAYERED | pw.WS_EX_TRANSPARENT)


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_the_window_region_is_the_hit_surface_in_windows_orientation(scale):
    width, height = W * scale, H * scale
    rects = pw.pet_region_rects(width, height)
    assert len(rects) == 3

    def inside(x, y):
        return any(left <= x < right and top <= y < bottom for left, top, right, bottom in rects)

    # Every point the hit policy calls the pet is inside the region, and the empty corners are not.
    for x in range(0, int(width), 4):
        for y in range(0, int(height), 4):
            if pw.point_hits_pet(x + 0.5, y + 0.5, (0.0, 0.0, width, height)):
                assert inside(x + 0.5, y + 0.5), (x, y)
    assert not inside(1, 1) and not inside(1, height - 2)
    canvas = rects[0]
    assert canvas[3] == round(height - pet_native.CANVAS_BOTTOM * scale), "canvas sits at the bottom"
    bubble = rects[1]
    assert bubble[3] >= canvas[1], "the bubble reaches down to its tail above the character"
    controls = rects[2]
    assert controls[1] <= 4 * scale + 1, "the controls are at the top"


def test_the_yield_cut_is_the_overlap_in_window_pixels():
    frame = (100.0, 100.0, W, H)
    assert pw.yield_cut(frame, None) is None
    assert pw.yield_cut(frame, (0.0, 0.0, 50.0, 50.0)) is None
    assert pw.yield_cut(frame, (150.0, 250.0, 1000.0, 40.0)) == (50, 150, int(W), 190)


def test_a_pet_dropped_out_of_the_window_is_centred_on_the_pointer():
    assert pw.cursor_centred_position((1000.0, 500.0), 1.0) == {"x": 1000.0 - W / 2, "y": 500.0 - H / 2}
    assert pw.cursor_centred_position((1000.0, 500.0), 2.0) == {"x": 1000.0 - W, "y": 500.0 - H}


# --------------------------------------------------------------------- fake user32 seam


class _FakeUser32:
    def __init__(self, api) -> None:
        self.api = api
        self.shows: list[int] = []
        self.focused: list[int] = []

    def ShowWindow(self, hwnd, cmd):
        self.shows.append(cmd)
        self.api.visible = cmd != pw.SW_HIDE
        return True

    def IsWindowVisible(self, hwnd):
        return self.api.visible if hwnd == 0x1234 else hwnd not in self.api.gone

    def IsWindow(self, hwnd):
        return hwnd not in self.api.gone

    def SetForegroundWindow(self, hwnd):
        self.focused.append(hwnd)
        self.api.fg = hwnd
        return True


class _FakeApi:
    """The `_Win32` surface, with a window that starts the way pywebview creates it."""

    def __init__(self, frame=(234.0, 234.0, 192.0, 197.0), areas=None, scale=1.0) -> None:
        self.frame = tuple(frame)
        self.style = 0x08050000
        self.areas = areas or [(0.0, 0.0, 1920.0, 1040.0)]
        self._scale = scale
        self.cursor_at = (5.0, 5.0)
        self.button = False
        self.visible = True
        self.moves: list[tuple] = []
        self.topmost = 0
        self.glass = 0
        self.regions: list[tuple] = []
        self.fg = 0x77          # the VOOL window has the keyboard
        self.gone: set[int] = set()  # window handles that have been closed
        self.user32 = _FakeUser32(self)

    def foreground(self):
        return self.fg

    def extend_glass(self, hwnd):
        self.glass += 1
        return True

    def set_region(self, hwnd, rects, cut=None):
        self.regions.append((tuple(rects), cut))
        return True

    def ex_style(self, hwnd):
        return self.style

    def set_ex_style(self, hwnd, value):
        self.style = value

    def rect(self, hwnd):
        return self.frame

    def move(self, hwnd, x, y):
        self.moves.append((x, y))
        self.frame = (x, y, self.frame[2], self.frame[3])

    def place(self, hwnd, frame):
        self.moves.append(tuple(frame[:2]))
        self.frame = tuple(frame)

    def scale(self, hwnd):
        return self._scale

    def raise_topmost(self, hwnd, extra=0):
        self.topmost += 1
        self.style |= pw.WS_EX_TOPMOST

    def cursor(self):
        return self.cursor_at

    def primary_button_down(self):
        return self.button

    def work_areas(self):
        return list(self.areas)


class _PetWindow:
    def __init__(self) -> None:
        self.native = types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x1234))
        self.events = types.SimpleNamespace(loaded=threading.Event())
        self.events.loaded.set()
        self.scripts: list[str] = []

    def evaluate_js(self, script):
        self.scripts.append(script)


@pytest.fixture
def api(monkeypatch):
    fake = _FakeApi()
    monkeypatch.setattr(pw, "_WIN32", fake)
    return fake


def _controller(window=None, focus_owner=0x77):
    controller = pw.WindowsPetWindowController(window or _PetWindow(), focus_owner)
    controller.ADOPT_TIMEOUT_SECONDS = 0.0
    return controller


def test_adopt_applies_the_policy_restores_full_size_and_shows_without_focus(api):
    controller = _controller()
    facts = controller.adopt(None)
    try:
        assert facts["ok"] is True
        assert api.frame[2:] == (W, H), "the pet must be its full size, not the short window pywebview made"
        assert api.frame == pw.default_pet_frame(api.areas, (W, H))
        assert api.style & pw.WS_EX_TOOLWINDOW and api.style & pw.WS_EX_NOACTIVATE
        assert api.style & pw.WS_EX_TOPMOST and not api.style & pw.WS_EX_APPWINDOW
        assert api.glass == 1, "the margin shows what is behind the pet"
        assert api.regions[-1] == (tuple(pw.pet_region_rects(W, H)), None), "only the pet takes clicks"
        assert api.user32.shows[-1] == pw.SW_SHOWNOACTIVATE, "shown without activation"
        assert facts["focus_returned"] is False, "nothing to hand back when the pet never took it"
        assert controller.stats()["running"] is True
    finally:
        controller.hide()


@pytest.mark.parametrize("taken_by", [0x1234, 0x99])
def test_the_keyboard_goes_back_if_creating_the_pet_moved_it(api, taken_by):
    """Measured: pywebview's show/hide/activate of a transparent window left the keyboard with the
    pet (0x1234) or with the next window in line, another app (0x99)."""
    controller = _controller()
    api.fg = taken_by
    try:
        assert controller.adopt(None)["focus_returned"] is True
        assert api.user32.focused == [0x77] and api.fg == 0x77
    finally:
        controller.hide()


def test_the_keyboard_never_goes_to_a_closed_window(api):
    """A closed window's handle can be reused by an unrelated one: never focus it."""
    controller = _controller()
    api.fg = 0x1234
    api.gone.add(0x77)
    try:
        assert controller.adopt(None)["focus_returned"] is False
        tracker = controller._tracker
        tracker.tick()
        assert api.user32.focused == []
    finally:
        controller.hide()


def test_no_owner_known_means_no_focus_change(api):
    controller = _controller(focus_owner=0)
    api.fg = 0x99
    try:
        assert controller.adopt(None)["focus_returned"] is False
        assert api.user32.focused == []
    finally:
        controller.hide()


def test_adopt_restores_a_saved_spot_at_high_dpi(monkeypatch):
    fake = _FakeApi(areas=[(0.0, 0.0, 2880.0, 1752.0)], scale=1.5)
    monkeypatch.setattr(pw, "_WIN32", fake)
    controller = _controller()
    try:
        facts = controller.adopt({"x": 900.0, "y": 600.0})
        assert facts["restored_saved_position"] is True
        assert fake.frame == (900.0, 600.0, W * 1.5, H * 1.5)
        assert controller.position() == {"x": 900.0, "y": 600.0}
    finally:
        controller.hide()


def test_no_window_handle_means_no_policy_and_an_honest_error(api):
    window = _PetWindow()
    window.native = None
    controller = _controller(window)
    assert controller.available is False
    assert controller.adopt(None) == {"ok": False, "error": "no_native_window"}
    assert controller.position() is None and controller.reset_position() is None


def test_hide_stops_polling_and_reset_only_moves_a_pet_on_the_desktop(api):
    controller = _controller()
    controller.adopt({"x": 100.0, "y": 100.0})
    controller.hide()
    assert api.user32.shows[-1] == pw.SW_HIDE
    assert controller.stats()["running"] is False, "a hidden pet costs nothing"
    assert controller.reset_position() is None, "a hidden pet is not put back on screen"
    controller.show()
    try:
        assert api.user32.shows[-1] == pw.SW_SHOWNOACTIVATE
        assert controller.reset_position() == {"x": 1920.0 - W - 48.0, "y": 1040.0 - H - 96.0}
    finally:
        controller.hide()


def test_the_yield_rect_is_scaled_and_validated(monkeypatch):
    fake = _FakeApi(scale=1.25)
    monkeypatch.setattr(pw, "_WIN32", fake)
    controller = _controller()
    assert controller.set_yield_rect({"x": 1, "y": 1, "width": 1, "height": 1})["ok"] is False  # before adopt
    controller.adopt(None)
    try:
        result = controller.set_yield_rect({"x": 100, "y": 700, "width": 400, "height": 80})
        assert result == {"ok": True, "rect": [125.0, 875.0, 500.0, 100.0]}
        assert controller.set_yield_rect(None) == {"ok": True, "cleared": True}
        assert controller.set_yield_rect({"x": "no", "y": 1, "width": 1, "height": 1}) == {"ok": False, "error": "invalid_rect"}
        assert controller.set_yield_rect({"x": 0, "y": 0, "width": 30000, "height": 1}) == {"ok": False, "error": "rect_too_large"}
    finally:
        controller.hide()


# --------------------------------------------------------------------- the tracker


def _tracker(api, **kw):
    return pw.WindowsPetWindowTracker(0x1234, api=api, **kw)


def test_a_drag_owns_the_geometry_and_the_drop_settles_once():
    api = _FakeApi(frame=(1900.0, 400.0, W, H))   # dragged mostly past the right edge
    drops: list[tuple] = []
    tracker = _tracker(api, on_drop=drops.append)
    api.button = True
    tracker.tick()
    tracker.tick()
    assert api.moves == [], "no clamp while the button is down"
    api.button = False
    tracker.tick()
    assert len(api.moves) == 1 and api.moves[0][0] <= 1920.0 - W
    assert drops == [api.moves[0]], "the settled spot is reported once, for persistence"
    tracker.tick()
    assert len(drops) == 1 and len(api.moves) == 1, "after the settle the position holds"


def test_an_idle_pet_off_every_display_is_rescued():
    api = _FakeApi(frame=(5000.0, 400.0, W, H))
    tracker = _tracker(api)
    tracker.tick()
    assert api.moves and 0.0 <= api.moves[0][0] <= 1920.0 - W


def test_hover_is_reported_on_change_only():
    api = _FakeApi(frame=(100.0, 100.0, W, H))
    seen: list[tuple] = []
    tracker = _tracker(api, on_interaction=lambda h, d: seen.append((h, d)) or True)
    api.cursor_at = _canvas_centre(api.frame)
    tracker.tick()
    tracker.tick()
    api.cursor_at = (5.0, 5.0)
    tracker.tick()
    assert seen == [(True, False), (False, False)]


def test_the_region_is_cut_only_over_the_yield_region_and_restored_on_stop():
    api = _FakeApi(frame=(100.0, 100.0, W, H))
    tracker = _tracker(api)
    tracker.tick()
    assert api.regions == [(tuple(pw.pet_region_rects(W, H)), None)]
    tracker.tick()
    assert len(api.regions) == 1, "the region is rebuilt on change only"
    tracker.set_yield_rect((0.0, 250.0, 2000.0, 60.0))      # a permission bar under the pet
    tracker.tick()
    assert api.regions[-1][1] == (0, 150, int(W), 210), "the bar's part of the pet lets clicks through"
    tracker.set_yield_rect(None)
    tracker.tick()
    assert api.regions[-1][1] is None
    tracker.set_yield_rect((0.0, 250.0, 2000.0, 60.0))
    tracker.tick()
    tracker.stop()
    assert api.regions[-1][1] is None, "a stopped tracker never leaves the pet cut"


def test_the_region_never_changes_mid_drag():
    api = _FakeApi(frame=(100.0, 100.0, W, H))
    tracker = _tracker(api)
    tracker.set_yield_rect((0.0, 250.0, 2000.0, 60.0))
    api.button = True
    tracker.tick()
    assert api.regions == []


def test_a_click_on_the_pet_never_keeps_the_keyboard():
    """Measured: the press of a real drag let WebView2 activate the pet despite NOACTIVATE."""
    api = _FakeApi(frame=(100.0, 100.0, W, H))
    tracker = _tracker(api, focus_owner=0x77)
    api.fg = 0x55                 # the operator moved to another app: that is now the owner
    tracker.tick()
    api.fg = 0x1234               # pressing on the pet activated it
    api.button = True
    tracker.tick()
    assert api.user32.focused == [], "never mid-press: a drag or a control click must finish"
    api.button = False
    tracker.tick()
    assert api.user32.focused == [0x55] and api.fg == 0x55
    assert tracker.stats()["keyboard_returns"] == 1

    class _Refusing(_FakeUser32):
        def SetForegroundWindow(self, hwnd):
            self.focused.append(hwnd)
            return False

    api.user32 = _Refusing(api)
    api.fg = 0x1234
    for _ in range(5):
        tracker.tick()
    assert api.user32.focused == [0x55], "a refused hand-back is tried once, not every tick"


def test_a_tracker_fault_never_escapes():
    api = _FakeApi()
    api.rect = lambda hwnd: (_ for _ in ()).throw(OSError("gone"))
    tracker = _tracker(api)
    tracker.tick()
    assert tracker.stats()["ticks"] == 1


# --------------------------------------------------------------------- host wiring


def test_a_winforms_window_gets_the_windows_controller(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(pet_native.controller_for(_PetWindow()), pw.WindowsPetWindowController)
    # Anything without a WinForms form keeps the existing controller (and its test seam).
    assert isinstance(pet_native.controller_for(object()), pet_native.PetWindowController)


def test_other_platforms_keep_the_existing_controller(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(pet_native.controller_for(_PetWindow()), pet_native.PetWindowController)


def test_the_pointer_drop_uses_the_windows_cursor(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pw, "cursor_position", lambda: {"x": 1.0, "y": 2.0})
    assert pet_native.cursor_position() == {"x": 1.0, "y": 2.0}


def test_detach_on_windows_builds_the_windows_controller_and_persists_the_drop(monkeypatch, api):
    from installer.bundle import vool_window
    from installer.bundle.vool_window import _WindowApi

    pet = _PetWindow()

    class _Webview:
        def create_window(self, *_args, **_kwargs):
            return pet

    class _Main:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def evaluate_js(self, script):
            self.scripts.append(script)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pw, "cursor_position", lambda: {"x": 700.0, "y": 300.0})
    monkeypatch.setattr(vool_window, "_log", lambda *_a, **_k: None)
    main = _Main()
    bridge = _WindowApi()
    bridge.set_window(main, _Webview())
    try:
        result = bridge.detach_companion({"state": "idle", "at_pointer": True})
        assert result == {"ok": True, "mode": "desktop"}
        assert isinstance(bridge._pet, pw.WindowsPetWindowController)
        assert bridge._pet._focus_owner == 0x77, "the keyboard owner is read before the pet exists"
        assert api.frame == (700.0, 300.0, W, H), "the pet lands where it was dropped, at full size"
        assert any("desktopMoved" in s and "700" in s for s in main.scripts), "the drop spot is saved"
        assert bridge.reset_companion_position()["ok"] is True
        assert bridge.hide_companion() == {"ok": True, "mode": "hidden"}
        assert api.visible is False and bridge.companion_overhead()["running"] is False
    finally:
        if bridge._pet is not None:
            bridge._pet.hide()
