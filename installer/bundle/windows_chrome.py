"""Windows window chrome for every window VOOL opens (vool_window._create_vool_window). Never the pet.

* A dark title bar: DWM's immersive dark mode attribute on the window's HWND (20; 19 on Windows 10
  builds before 20H1), which also makes the minimise, maximise and close glyphs white, plus the
  caption colour (35, Windows 11 only) set to VOOL's dark panel. Owner hand test, Windows 11: the
  title bar was plain white over VOOL's dark page.
* VOOL's own logo instead of Python's: the window host is pythonw.exe, so pywebview's WinForms
  form took pythonw's icon and the taskbar grouped the window under pythonw. The process claims a
  stable explicit AppUserModelID (the installer's launcher shortcuts carry the same one), and the
  window icon is the vool.ico the installer gives the desktop shortcut, installed next to the
  window host ({app}\\vool.ico) and shipped in the source tree as installer/assets/vool.ico.

The frameless pet has no title bar and its own window policy (``pet_native_windows``); nothing
here is ever applied to it. Everything is presentation, Windows only, and a failure anywhere is a
silent no-op: a window that cannot be styled still opens exactly as before.
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
# core/ui_palette.py DARK_PALETTE_CSS --panel: the colour of VOOL's own dark chrome.
CAPTION_COLOR_HEX = "#1b1b1e"
# The taskbar identity. vool.iss [Icons] gives the launcher shortcuts the same AppUserModelID.
APP_USER_MODEL_ID = "Parad0xLabs.VOOL"
ICON_FILENAME = "vool.ico"
_HERE = Path(__file__).resolve().parent
_ASSET_ICON = _HERE.parent / "assets" / ICON_FILENAME
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
# How long a styler waits for pywebview to build the form behind a new window.
HWND_WAIT_SECONDS = 10.0


def is_windows(platform: str | None = None) -> bool:
    return (platform if platform is not None else sys.platform) == "win32"


def colorref(hex_colour: str) -> int:
    """``#rrggbb`` as a Win32 COLORREF (0x00BBGGRR)."""
    value = hex_colour.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return (blue << 16) | (green << 8) | red


def set_app_user_model_id(*, shell32: object = None, platform: str | None = None) -> bool:
    """Claim VOOL's AppUserModelID for this process. Call before any window; never raises."""
    if not is_windows(platform):
        return False
    try:
        import ctypes

        shell = shell32 if shell32 is not None else ctypes.WinDLL("shell32")
        return int(shell.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_USER_MODEL_ID))) == 0
    except Exception:
        return False


def icon_path(here: object = None) -> str | None:
    """VOOL's .ico: next to the window host when installed ({app}\\vool.ico, where the installer
    puts the desktop shortcut's icon), else the source tree's installer/assets/vool.ico."""
    folder = Path(here) if here is not None else Path(sys.argv[0] or ".").resolve().parent
    for candidate in (folder / ICON_FILENAME, _ASSET_ICON):
        with contextlib.suppress(OSError):
            if candidate.is_file():
                return str(candidate)
    return None


def start_icon_options(*, platform: str | None = None, here: object = None) -> dict:
    """``webview.start`` keywords that give every pywebview form VOOL's icon (WinForms reads it
    for each window it builds). Empty off Windows or when no icon is found; never raises."""
    if not is_windows(platform):
        return {}
    try:
        found = icon_path(here)
    except Exception:
        return {}
    return {"icon": found} if found else {}


def set_window_icon(hwnd: object, path: str | None = None, *, user32: object = None) -> bool:
    """Give ``hwnd`` the icon at ``path`` (VOOL's: ``icon_path()``), small (title bar) and big
    (taskbar, Alt+Tab). No path, no icon. Never raises."""
    try:
        if not hwnd or int(hwnd) <= 0:
            return False
        if not path:
            return False
        import ctypes

        api = user32 if user32 is not None else ctypes.WinDLL("user32")
        done = 0
        for which, cx, cy in ((ICON_SMALL, SM_CXSMICON, SM_CYSMICON), (ICON_BIG, SM_CXICON, SM_CYICON)):
            handle = api.LoadImageW(None, ctypes.c_wchar_p(str(path)), IMAGE_ICON,
                                    int(api.GetSystemMetrics(cx)), int(api.GetSystemMetrics(cy)), LR_LOADFROMFILE)
            if handle:
                api.SendMessageW(int(hwnd), WM_SETICON, which, handle)
                done += 1
        return done == 2
    except Exception:
        return False


def _dwmapi():
    import ctypes

    return ctypes.WinDLL("dwmapi")


def _set_attribute(dwm, hwnd: int, attribute: int, value: int) -> bool:
    import ctypes

    data = ctypes.c_int(int(value))
    return int(dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))) == 0


def apply_dark_title_bar(hwnd: object, *, dwmapi: object = None) -> bool:
    """Ask DWM for a dark title bar on ``hwnd``. True when dark mode took; never raises."""
    try:
        if not hwnd or int(hwnd) <= 0:
            return False
        dwm = dwmapi if dwmapi is not None else _dwmapi()
        dark = _set_attribute(dwm, int(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or _set_attribute(
            dwm, int(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, 1)
        if dark:
            with contextlib.suppress(Exception):   # Windows 10 has no caption colour: keep dark mode
                _set_attribute(dwm, int(hwnd), DWMWA_CAPTION_COLOR, colorref(CAPTION_COLOR_HEX))
        return bool(dark)
    except Exception:
        return False


def _hwnd(pywebview_window: object, wait: float) -> int | None:
    from installer.bundle.pet_native_windows import hwnd_for

    return hwnd_for(pywebview_window, wait)


def style_native_window(pywebview_window: object, *, platform: str | None = None,
                        background: bool = True, wait: float = HWND_WAIT_SECONDS) -> bool:
    """Dark title bar and VOOL's icon on one VOOL window once its HWND exists. False off Windows;
    never raises.

    pywebview builds the form on its GUI thread, so by default this waits for the handle on a
    short daemon thread and the caller never blocks."""
    if not is_windows(platform):
        return False

    def _apply() -> None:
        hwnd = None
        with contextlib.suppress(Exception):
            hwnd = _hwnd(pywebview_window, wait)
        if not hwnd:
            return
        with contextlib.suppress(Exception):
            apply_dark_title_bar(hwnd)
        with contextlib.suppress(Exception):
            set_window_icon(hwnd, icon_path())

    try:
        if background:
            threading.Thread(target=_apply, name="vool-window-chrome", daemon=True).start()
        else:
            _apply()
    except Exception:
        pass
    return True
