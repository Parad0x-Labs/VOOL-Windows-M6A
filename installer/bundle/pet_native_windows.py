"""Windows corrections for the detached pixel pet: the Win32 half of ``pet_native``.

``pet_native`` gives the Mac pet its desktop behaviour through AppKit: a floating level, no focus
steal, click-through margins, a drop that holds, a position that survives a restart. None of that
reaches the WinForms/WebView2 window pywebview opens on Windows, so there the pet came up as an
ordinary window: a taskbar button, not above other apps, its empty margin catching clicks, no
saved position. This module applies the same policy through Win32 and keeps the same controller
surface, so ``vool_window.py`` only picks which controller to build.

Two halves, as in ``pet_native``:

* **Pure geometry** (no Win32 import): Win32 screen coordinates are pixels with the origin at the
  top-left of the primary display and y growing DOWN; the shared hit regions are logical points
  with y growing UP. The conversion lives here and is testable anywhere.
* **A thin native layer** (ctypes, Windows only): window styles, top-most order, show without
  activation, see-through margins, the window region that decides what takes a click, and the
  on-screen safety net, on one bounded polling thread that stops whenever the pet is hidden.

How the margins are made see-through and click-through was chosen by measurement on Windows 11
(probe, 7 Nov 2026, pixels and ``WindowFromPoint`` over a known backdrop): pywebview's own
``transparent`` leaves a grey box; a colour key hides the box but lets clicks through the
character too (WebView2 does not draw into the window's own surface); a black form under a DWM
frame extended over the whole client area shows the backdrop through the margins, and a window
region made of the pet's hit rects then hands every margin click to the window underneath while
the character keeps it. That last pair is what this module applies.

Everything here is presentation. Nothing in this module reads or writes user data, runs a task, or
carries any authority. It touches only the pet's own window handle.
"""
from __future__ import annotations

import contextlib
import math
import sys
import threading
import time

from installer.bundle import pet_native

Rect = tuple[float, float, float, float]

# Extended window styles and SetWindowPos flags, restated as plain numbers so the policy is
# assertable without Windows.
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
RGN_OR = 2
RGN_DIFF = 4
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
SM_SWAPBUTTON = 23

# The pet's window policy as ex-style bits: never activates, no taskbar or Alt+Tab entry.
# WS_EX_APPWINDOW is removed so the shell cannot force a button; LAYERED/TRANSPARENT are removed
# because a layered pet hit-tests by its own surface, which WebView2 does not draw into.
PET_EX_STYLE_SET = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
PET_EX_STYLE_CLEAR = WS_EX_APPWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT

# Default placement inset from the primary work area's lower-right corner, in logical points.
# The Mac pet sits 48 in from the right and 96 up from the bottom; the same on Windows.
DEFAULT_INSET_RIGHT = 48.0
DEFAULT_INSET_BOTTOM = 96.0


# --------------------------------------------------------------------------- pure geometry


def pet_ex_style(current: int) -> int:
    """The ex-style the pet's window must carry, from whatever it carries now."""
    return (int(current) | PET_EX_STYLE_SET) & ~PET_EX_STYLE_CLEAR


def pet_region_rects(width: float, height: float) -> list[tuple[int, int, int, int]]:
    """The window region, as window-local (left, top, right, bottom) pixels, top-left origin.

    The shared hit rects (bottom-left origin, logical points) flipped and scaled to this window.
    Outside the region Windows neither draws the window nor gives it the click, so the region IS
    the pet's hit surface. The bubble's rect is extended down by the gap to its tail, so the
    tail that points at the character is not clipped.
    """
    scale = frame_scale((0.0, 0.0, width, height))
    logical_h = height / scale
    rects = []
    for index, (x, y, w, h) in enumerate(pet_native.pet_hit_rects(width / scale, logical_h)):
        if index == 1:  # the bubble band: include the gap holding its tail
            y -= float(pet_native.BUBBLE_GAP)
            h += float(pet_native.BUBBLE_GAP)
        top = logical_h - y - h
        rects.append((
            math.floor(x * scale), math.floor(top * scale),
            math.ceil((x + w) * scale), math.ceil((top + h) * scale),
        ))
    return rects


def yield_cut(frame: Rect, avoid_rect: Rect | None) -> tuple[int, int, int, int] | None:
    """The part of the operator-answer region that lies over the pet, window-local, or None.

    It is cut out of the window region, so a permission bar under the pet takes the click there.
    """
    if avoid_rect is None:
        return None
    fx, fy, fw, fh = frame
    ax, ay, aw, ah = avoid_rect
    left, top = max(ax, fx) - fx, max(ay, fy) - fy
    right, bottom = min(ax + aw, fx + fw) - fx, min(ay + ah, fy + fh) - fy
    if right <= left or bottom <= top:
        return None
    return (math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom))


def frame_scale(frame: Rect) -> float:
    """Pixels per logical point for the pet's window, read from its real size.

    pywebview sizes the window as logical size x DPI scale, so its own width says the scale; no
    second DPI source is consulted that could disagree with the window that is actually there.
    """
    width = float(frame[2])
    if not math.isfinite(width) or width <= 0:
        return 1.0
    return width / float(pet_native.PET_WINDOW_WIDTH)


def point_hits_pet(screen_x: float, screen_y: float, frame: Rect, avoid_rect: Rect | None = None) -> bool:
    """Win32 twin of ``pet_native.point_hits_pet``: would a click at this screen pixel land on the
    pet? ``frame`` and ``avoid_rect`` are Win32 screen rects (top-left origin, pixels)."""
    if avoid_rect is not None:
        ax, ay, aw, ah = avoid_rect
        if ax <= screen_x <= ax + aw and ay <= screen_y <= ay + ah:
            return False
    fx, fy, fw, fh = frame
    scale = frame_scale(frame)
    width = fw / scale
    height = fh / scale
    local_x = (screen_x - fx) / scale
    # Shared hit rects use a bottom-left origin; Win32 local y grows down from the top edge.
    local_y = height - (screen_y - fy) / scale
    return any(pet_native._point_in_rect(local_x, local_y, rect) for rect in pet_native.pet_hit_rects(width, height))


def primary_area(work_areas: list[Rect]) -> Rect | None:
    """The primary display's work area: on Windows the primary display holds the origin."""
    for area in work_areas:
        x, y, w, h = area
        if x <= 0.0 < x + w and y <= 0.0 < y + h:
            return area
    return work_areas[0] if work_areas else None


def default_pet_frame(work_areas: list[Rect], size: tuple[float, float]) -> Rect:
    """Where a pet with no saved position goes: lower-right of the primary work area, inset."""
    width, height = float(size[0]), float(size[1])
    area = primary_area(work_areas)
    if area is None:
        return (0.0, 0.0, width, height)
    scale = width / float(pet_native.PET_WINDOW_WIDTH) if width > 0 else 1.0
    sx, sy, sw, sh = area
    return (
        sx + sw - width - DEFAULT_INSET_RIGHT * scale,
        sy + sh - height - DEFAULT_INSET_BOTTOM * scale,
        width,
        height,
    )


def placed_frame(saved: object, work_areas: list[Rect], size: tuple[float, float]) -> tuple[Rect, bool]:
    """A saved {x, y} that is on a display today, or the default spot. Returns (frame, restored)."""
    restored = pet_native.restore_saved_position(saved, work_areas, size)
    if restored is not None:
        return restored, True
    return default_pet_frame(work_areas, size), False


def page_rect_to_screen(payload: object, scale: float) -> Rect | None:
    """The chat page's operator-answer rect (CSS pixels, top-left origin) as Win32 pixels.

    The page measures with ``window.screenX`` and ``getBoundingClientRect``, both CSS pixels; the
    pet's window lives in device pixels. Same validation as the Mac seam: finite, positive, bounded.
    """
    if not isinstance(payload, dict):
        return None
    try:
        x = float(payload.get("x"))
        y = float(payload.get("y"))
        width = float(payload.get("width"))
        height = float(payload.get("height"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
        return None
    if width > 20000 or height > 20000:
        return None
    s = scale if math.isfinite(scale) and scale > 0 else 1.0
    return (x * s, y * s, width * s, height * s)


def cursor_centred_position(cursor: tuple[float, float], scale: float) -> dict:
    """The window origin that centres the pet's window on the cursor (Win32 pixels)."""
    cx, cy = cursor
    return {
        "x": float(cx) - pet_native.PET_WINDOW_WIDTH * scale / 2.0,
        "y": float(cy) - pet_native.PET_WINDOW_HEIGHT * scale / 2.0,
    }


# --------------------------------------------------------------------------- native layer


class _Win32:
    """The handful of user32 calls the pet needs, bound with explicit types (64-bit handles)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        self.user32 = user32
        gdi32 = ctypes.WinDLL("gdi32")
        gdi32.CreateRectRgn.restype = wintypes.HRGN
        gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32 = gdi32

        class MARGINS(ctypes.Structure):
            _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                        ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

        self.MARGINS = MARGINS
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(MARGINS)]
        self.dwm = dwm

    def extend_glass(self, hwnd: int) -> bool:
        """Compose the whole client area with its alpha, so the black form shows what is behind."""
        margins = self.MARGINS(-1, -1, -1, -1)
        return int(self.dwm.DwmExtendFrameIntoClientArea(hwnd, self.ctypes.byref(margins))) == 0

    def set_region(self, hwnd: int, rects, cut=None) -> bool:
        """Make the window exactly these rects (minus ``cut``). Windows owns the region after."""
        gdi = self.gdi32
        region = gdi.CreateRectRgn(0, 0, 0, 0)
        for left, top, right, bottom in rects:
            part = gdi.CreateRectRgn(left, top, right, bottom)
            gdi.CombineRgn(region, region, part, RGN_OR)
            gdi.DeleteObject(part)
        if cut is not None:
            part = gdi.CreateRectRgn(*cut)
            gdi.CombineRgn(region, region, part, RGN_DIFF)
            gdi.DeleteObject(part)
        if self.user32.SetWindowRgn(hwnd, region, True):
            return True
        gdi.DeleteObject(region)
        return False

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def ex_style(self, hwnd: int) -> int:
        return int(self.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF

    def set_ex_style(self, hwnd: int, value: int) -> None:
        self.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, int(value))

    def rect(self, hwnd: int) -> Rect:
        r = self.wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, self.ctypes.byref(r)):
            raise OSError("GetWindowRect failed")
        return (float(r.left), float(r.top), float(r.right - r.left), float(r.bottom - r.top))

    def move(self, hwnd: int, x: float, y: float) -> None:
        self.user32.SetWindowPos(hwnd, None, round(x), round(y), 0, 0,
                                 SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def place(self, hwnd: int, frame: Rect) -> None:
        x, y, w, h = frame
        self.user32.SetWindowPos(hwnd, None, round(x), round(y), round(w), round(h),
                                 SWP_NOZORDER | SWP_NOACTIVATE)

    def scale(self, hwnd: int) -> float:
        """Pixels per logical point for this window, from the DPI Windows assigned it."""
        with contextlib.suppress(Exception):
            dpi = int(self.user32.GetDpiForWindow(hwnd))
            if dpi > 0:
                return dpi / 96.0
        return 1.0

    def raise_topmost(self, hwnd: int, extra: int = 0) -> None:
        self.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | extra)

    def cursor(self) -> tuple[float, float]:
        p = self.wintypes.POINT()
        if not self.user32.GetCursorPos(self.ctypes.byref(p)):
            raise OSError("GetCursorPos failed")
        return (float(p.x), float(p.y))

    def primary_button_down(self) -> bool:
        key = VK_RBUTTON if self.user32.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON
        return bool(self.user32.GetAsyncKeyState(key) & 0x8000)

    def work_areas(self) -> list[Rect]:
        """Every display's work area (taskbar excluded), in screen pixels."""
        ctypes, wintypes = self.ctypes, self.wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        areas: list[Rect] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def _each(monitor, _dc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                w = info.rcWork
                areas.append((float(w.left), float(w.top), float(w.right - w.left), float(w.bottom - w.top)))
            return True

        callback = proc_type(_each)
        self.user32.EnumDisplayMonitors(None, None, callback, 0)
        return areas


_WIN32: _Win32 | None = None


def win32() -> _Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


def hwnd_for(pywebview_window, timeout: float = 0.0) -> int | None:
    """The real HWND behind a pywebview window, or None if the backend is not WinForms.

    pywebview builds the form on its GUI thread and the bridge that asks for the pet runs on a
    worker thread, so the form may not exist yet when ``create_window`` returns: wait for it.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        native = getattr(pywebview_window, "native", None)
        handle = getattr(native, "Handle", None) if native is not None else None
        if handle is not None:
            with contextlib.suppress(Exception):
                value = int(handle.ToInt64())
                if value:
                    return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def apply_glass(pywebview_window, hwnd: int) -> bool:
    """Make the pet's empty margin show whatever is behind it.

    WebView2 paints a transparent page over the form, so the form's own colour shows where the
    page draws nothing (pywebview leaves it grey). A black form composed by DWM with the frame
    extended over the whole client area reads as clear there; the character is drawn on top.
    """
    form = getattr(pywebview_window, "native", None)
    if form is not None:
        with contextlib.suppress(Exception):
            from System import Action  # pythonnet, loaded by pywebview's WinForms backend
            from System.Drawing import Color

            def _paint_black():
                form.BackColor = Color.FromArgb(0, 0, 0)

            form.Invoke(Action(_paint_black))
    return win32().extend_glass(hwnd)


def configure_pet_window(pywebview_window, hwnd: int, frame: Rect) -> dict:
    """Apply the pet's window policy and report what the OS now says about the window."""
    api = win32()
    api.set_ex_style(hwnd, pet_ex_style(api.ex_style(hwnd)))
    glass = apply_glass(pywebview_window, hwnd)
    region = api.set_region(hwnd, pet_region_rects(frame[2], frame[3]))
    # SWP_FRAMECHANGED makes the shell re-read the styles (taskbar/Alt+Tab membership).
    api.raise_topmost(hwnd, SWP_FRAMECHANGED)
    style = api.ex_style(hwnd)
    return {
        "ex_style": hex(style),
        "topmost": bool(style & WS_EX_TOPMOST),
        "noactivate": bool(style & WS_EX_NOACTIVATE),
        "toolwindow": bool(style & WS_EX_TOOLWINDOW),
        "appwindow": bool(style & WS_EX_APPWINDOW),
        "glass": glass,
        "region": region,
    }


def show_without_focus(hwnd: int) -> None:
    """Show the pet above other windows without taking the keyboard from whatever has it."""
    api = win32()
    api.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    api.raise_topmost(hwnd, SWP_SHOWWINDOW)


def owner_still_there(api: _Win32, owner: int) -> bool:
    """Only hand the keyboard to a window that still exists and is shown: a closed window's
    handle can be reused by an unrelated one."""
    with contextlib.suppress(Exception):
        return bool(api.user32.IsWindow(owner)) and bool(api.user32.IsWindowVisible(owner))
    return False


class WindowsPetWindowTracker:
    """Hover reporting, the window region and the on-screen safety net, on one polling thread.

    Same rules as ``pet_native.PetWindowTracker``: a drag owns the geometry until the button comes
    up (``settle_decision`` says "hold"), the clamp runs once on release and the settled position
    is reported through ``on_drop``, and what takes a click only changes while the button is up.
    Click-through itself needs no polling here: the window region decides it in the OS.
    """

    def __init__(self, hwnd: int, interval: float = pet_native.TRACK_INTERVAL_SECONDS,
                 on_drop=None, on_interaction=None, api: _Win32 | None = None,
                 focus_owner: int = 0) -> None:
        self._hwnd = hwnd
        # The last window other than the pet that had the keyboard. A click or drag on the pet
        # lets WebView2 activate it despite WS_EX_NOACTIVATE (measured: the press of a real drag
        # left the pet in front); once the button is up the keyboard goes back here.
        self._keyboard_owner = int(focus_owner or 0)
        self._keyboard_returns = 0
        self._keyboard_tried = False
        self._interval = float(interval)
        self._api = api
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ticks = 0
        self._tick_seconds = 0.0
        self._clamps = 0
        self._dragging = False
        self._pet_drag = False
        self._interaction = None
        self._on_drop = on_drop
        self._on_interaction = on_interaction
        self._yield_rect: Rect | None = None
        self._region_key = None

    @property
    def api(self) -> _Win32:
        if self._api is None:
            self._api = win32()
        return self._api

    def set_yield_rect(self, rect: Rect | None) -> None:
        self._yield_rect = tuple(rect) if rect else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop = threading.Event()
            stop = self._stop
            self._thread = threading.Thread(target=self._run, args=(stop,), name="vool-pet-tracker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop polling. A hidden pet does no work, and is never left click-through."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._interaction = None
        self._pet_drag = False
        self._dragging = False
        # A re-show starts from the pet's full region, never one cut for a bar that has gone.
        with contextlib.suppress(Exception):
            if self._region_key is not None and self._region_key[1] is not None:
                api = self.api
                _x, _y, w, h = api.rect(self._hwnd)
                api.set_region(self._hwnd, pet_region_rects(w, h))
        self._region_key = None

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self._interval):
            self.tick()

    def tick(self) -> None:
        started = time.perf_counter()
        try:
            api = self.api
            frame = api.rect(self._hwnd)
            dragging = api.primary_button_down()
            cx, cy = api.cursor()
            hovered = point_hits_pet(cx, cy, frame, self._yield_rect)
            if dragging and not self._dragging:
                self._pet_drag = hovered
            if not dragging:
                self._pet_drag = False
            interaction = (hovered, self._pet_drag)
            if interaction != self._interaction and self._on_interaction is not None:
                if self._on_interaction(*interaction):
                    self._interaction = interaction
            action, corrected = pet_native.settle_decision(
                frame, api.work_areas(), button_down=dragging, drag_seen=self._dragging
            )
            self._dragging = dragging
            if not dragging:
                self._keep_keyboard_away()
            if action in ("settle", "rescue"):
                if corrected[:2] != frame[:2]:
                    api.move(self._hwnd, corrected[0], corrected[1])
                    self._clamps += 1
                if action == "settle" and self._on_drop is not None:
                    self._on_drop((corrected[0], corrected[1]))
            elif action == "idle":
                self._sync_region(frame)
        except Exception:
            # A tracker fault must never take the app down; the pet keeps its last state.
            pass
        finally:
            self._ticks += 1
            self._tick_seconds += time.perf_counter() - started

    def _keep_keyboard_away(self) -> None:
        """Track who has the keyboard; if the pet has it, give it back (button up only, so a drag
        or a click on a control is never cut short)."""
        api = self.api
        current = api.foreground()
        if current != self._hwnd:
            if current:
                self._keyboard_owner = current
            self._keyboard_tried = False
        elif self._keyboard_owner and not self._keyboard_tried and owner_still_there(api, self._keyboard_owner):
            # One attempt per activation: if Windows refuses (foreground lock), don't retry
            # every tick; the next click on the pet tries again.
            self._keyboard_tried = True
            if api.user32.SetForegroundWindow(self._keyboard_owner):
                self._keyboard_returns += 1
                self._keyboard_tried = False

    def _sync_region(self, frame: Rect) -> None:
        """Rebuild the window region when the pet's size or the yield cut over it changed."""
        cut = yield_cut(frame, self._yield_rect)
        key = (tuple(frame[2:]), cut)
        if key == self._region_key:
            return
        if self.api.set_region(self._hwnd, pet_region_rects(frame[2], frame[3]), cut):
            self._region_key = key

    def apply_for_cursor(self, cursor_x: float, cursor_y: float, frame: Rect | None = None) -> bool:
        """Would a click at this point land on the pet (the region decides it in the OS)?"""
        if frame is None:
            frame = self.api.rect(self._hwnd)
        self._sync_region(frame)
        return point_hits_pet(cursor_x, cursor_y, frame, self._yield_rect)

    def stats(self) -> dict:
        return {
            "running": self.running,
            "interval_seconds": self._interval,
            "ticks": self._ticks,
            "total_tick_seconds": round(self._tick_seconds, 6),
            "mean_tick_ms": round((self._tick_seconds / self._ticks) * 1000.0, 4) if self._ticks else 0.0,
            "onscreen_corrections": self._clamps,
            "keyboard_returns": self._keyboard_returns,
        }


class WindowsPetWindowController:
    """``pet_native.PetWindowController``'s surface, for the WinForms/WebView2 pet window."""

    ADOPT_TIMEOUT_SECONDS = 5.0
    # pywebview shows a transparent window on its first navigation and activates it then; the
    # pet's policy is applied after that load so the show it does cannot undo it.
    LOAD_TIMEOUT_SECONDS = 5.0

    def __init__(self, pywebview_window, focus_owner: int = 0) -> None:
        self._pywebview_window = pywebview_window
        self._hwnd: int | None = None
        self._tracker: WindowsPetWindowTracker | None = None
        self._visible = False
        self.on_position_settled = None
        # Whoever had the keyboard before the pet was created (the host reads it first, normally
        # the VOOL window the pet was dragged out of). pywebview shows, hides and on its first
        # navigation activates a transparent window, which moves the keyboard; a pet must not,
        # so after the load the keyboard is handed back to this window.
        self._focus_owner = int(focus_owner or 0)

    def _window(self, timeout: float = 0.0) -> int | None:
        if self._hwnd is None:
            self._hwnd = hwnd_for(self._pywebview_window, timeout)
        return self._hwnd

    @property
    def available(self) -> bool:
        return self._window() is not None

    def _size(self) -> tuple[float, float]:
        """The pet's intended size in this window's pixels.

        Not the window's current size: pywebview sizes the form before it drops the frame, so a
        frameless pet comes up short by the frame it lost (measured on Windows 11: 192x197 for a
        208x236 pet at 100%), cutting into the bubble and the controls. ``adopt`` restores it.
        """
        hwnd = self._window()
        scale = win32().scale(hwnd) if hwnd is not None else 1.0
        return (pet_native.PET_WINDOW_WIDTH * scale, pet_native.PET_WINDOW_HEIGHT * scale)

    def adopt(self, saved_position: object = None) -> dict:
        """Apply the pet's window policy, place it, and start the tracker."""
        hwnd = self._window(self.ADOPT_TIMEOUT_SECONDS)
        if hwnd is None:
            return {"ok": False, "error": "no_native_window"}
        api = win32()
        loaded = getattr(getattr(self._pywebview_window, "events", None), "loaded", None)
        if loaded is not None:
            with contextlib.suppress(Exception):
                loaded.wait(self.LOAD_TIMEOUT_SECONDS)
        frame, restored = placed_frame(saved_position, api.work_areas(), self._size())
        api.place(hwnd, frame)
        facts = configure_pet_window(self._pywebview_window, hwnd, frame)
        show_without_focus(hwnd)
        self._visible = True
        facts["focus_returned"] = self._return_focus(hwnd)
        facts["frame"] = list(frame)
        facts["restored_saved_position"] = restored
        if self._tracker is None:
            self._tracker = WindowsPetWindowTracker(
                hwnd, on_drop=self._report_settled, on_interaction=self._report_interaction,
                focus_owner=self._focus_owner)
        self._tracker.start()
        facts["ok"] = True
        return facts

    def _return_focus(self, hwnd: int) -> bool:
        """If creating the pet moved the keyboard, give it back to the window that had it."""
        api = win32()
        owner = self._focus_owner
        if not owner or owner == hwnd or api.foreground() == owner or not owner_still_there(api, owner):
            return False
        return bool(api.user32.SetForegroundWindow(owner))

    def _report_interaction(self, hovered: bool, dragging: bool) -> bool:
        """Tell the pet page whether the pointer is over it, so its controls show on hover.

        Runs on the tracker thread, never the GUI thread, so pywebview's blocking evaluate_js
        is safe here; the page's own pointerenter cannot see a margin outside the region.
        """
        loaded = getattr(getattr(self._pywebview_window, "events", None), "loaded", None)
        if loaded is None or not loaded.is_set():
            return False
        script = ("window.VoolDesktopCompanion&&window.VoolDesktopCompanion.interaction("
                  + str(bool(hovered)).lower() + "," + str(bool(dragging)).lower() + ")")
        self._pywebview_window.evaluate_js(script)
        return True

    def _report_settled(self, origin: tuple[float, float]) -> None:
        """A drag settled: hand the final position to the host's persistence on a short worker
        thread, so the host's evaluate_js on the chat window never runs on the tracker tick."""
        if self.on_position_settled is None:
            return
        callback, payload = self.on_position_settled, tuple(origin)

        def _persist():
            with contextlib.suppress(Exception):
                callback(payload)

        threading.Thread(target=_persist, name="vool-pet-drop-settle", daemon=True).start()

    def set_yield_rect(self, payload: object) -> dict:
        if self._tracker is None:
            return {"ok": False, "error": "pet_not_active"}
        if payload is None:
            self._tracker.set_yield_rect(None)
            return {"ok": True, "cleared": True}
        rect = page_rect_to_screen(payload, frame_scale((0.0, 0.0, *self._size())))
        if rect is None:
            data = payload if isinstance(payload, dict) else {}
            try:
                too_large = float(data.get("width")) > 20000 or float(data.get("height")) > 20000
            except (TypeError, ValueError):
                too_large = False
            return {"ok": False, "error": "rect_too_large" if too_large else "invalid_rect"}
        self._tracker.set_yield_rect(rect)
        return {"ok": True, "rect": list(rect)}

    def show(self) -> None:
        hwnd = self._window()
        if hwnd is None:
            return
        show_without_focus(hwnd)
        self._visible = True
        if self._tracker is not None:
            self._tracker.start()

    def hide(self) -> None:
        """Hide the pet and stop all polling. A hidden pet costs nothing."""
        if self._tracker is not None:
            self._tracker.stop()
        hwnd = self._window()
        if hwnd is not None:
            win32().user32.ShowWindow(hwnd, SW_HIDE)
        self._visible = False

    def position(self) -> dict | None:
        hwnd = self._window()
        if hwnd is None:
            return None
        try:
            x, y, _w, _h = win32().rect(hwnd)
        except Exception:
            return None
        return {"x": x, "y": y}

    def reset_position(self) -> dict | None:
        """Put a pet that is on the desktop back at the default spot on the primary display."""
        hwnd = self._window()
        if hwnd is None:
            return None
        api = win32()
        if not api.user32.IsWindowVisible(hwnd):
            return None
        frame = default_pet_frame(api.work_areas(), self._size())
        api.place(hwnd, frame)
        show_without_focus(hwnd)
        return {"x": frame[0], "y": frame[1]}

    def apply_for_cursor(self, cursor_x: float, cursor_y: float) -> bool:
        if self._tracker is None:
            return False
        return self._tracker.apply_for_cursor(cursor_x, cursor_y)

    def stats(self) -> dict:
        return self._tracker.stats() if self._tracker is not None else {"running": False, "ticks": 0}


def cursor_position() -> dict | None:
    """The origin that centres a newly detached pet on the real desktop cursor."""
    if sys.platform != "win32":
        return None
    try:
        api = win32()
        cursor = api.cursor()
        scale = 1.0
        with contextlib.suppress(Exception):
            scale = float(api.user32.GetDpiForSystem()) / 96.0 or 1.0
        return cursor_centred_position(cursor, scale)
    except Exception:
        return None
