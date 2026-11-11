"""On Windows, VOOL's taskbar button and title bar show VOOL's logo, not Python's.

Owner hand test, Windows 11: the taskbar button and the top-left of the title bar showed the
Python logo. The window host is pythonw.exe, so with no icon of its own pywebview's WinForms form
takes pythonw's (ExtractIconW on sys.executable), and the taskbar groups the window under
pythonw's identity. Two parts:

(a) the process takes a stable explicit AppUserModelID before any window, and the installer's
    launcher shortcuts carry the same one, so the taskbar groups VOOL under its own shortcut;
(b) the window icon is the same vool.ico the installer gives the desktop shortcut, handed to
    pywebview (``webview.start(icon=...)``, which its WinForms backend applies to every form), and
    that .ico is really shipped next to the installed vool_window.py.

All rows are host-independent: Win32 calls go to recording stand-ins and the installer files are
read statically.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from installer.bundle import windows_chrome as wc

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "bundle" / "vool.iss"
BUILD = ROOT / "installer" / "bundle" / "build_bundle.ps1"


class _Shell32:
    def __init__(self, result=0, raises=False) -> None:
        self.ids: list[str] = []
        self.result = result
        self.raises = raises

    def SetCurrentProcessExplicitAppUserModelID(self, value):
        if self.raises:
            raise OSError("no shell32")
        self.ids.append(getattr(value, "value", value))
        return self.result


# --- (a) taskbar identity -----------------------------------------------------------------------


def test_the_app_user_model_id_is_stable_and_well_formed() -> None:
    assert wc.APP_USER_MODEL_ID == "Parad0xLabs.VOOL"
    assert len(wc.APP_USER_MODEL_ID) <= 128 and " " not in wc.APP_USER_MODEL_ID


def test_the_process_claims_the_id_on_windows_only() -> None:
    shell = _Shell32()
    assert wc.set_app_user_model_id(shell32=shell, platform="darwin") is False
    assert wc.set_app_user_model_id(shell32=shell, platform="linux") is False
    assert shell.ids == []
    assert wc.set_app_user_model_id(shell32=shell, platform="win32") is True
    assert shell.ids == ["Parad0xLabs.VOOL"]


def test_claiming_the_id_never_raises() -> None:
    assert wc.set_app_user_model_id(shell32=_Shell32(raises=True), platform="win32") is False
    assert wc.set_app_user_model_id(shell32=_Shell32(result=-2147024809), platform="win32") is False


def test_the_id_is_claimed_before_any_window() -> None:
    from installer.bundle import vool_window as nw

    main = inspect.getsource(nw.main)
    claimed = main.index("windows_chrome.set_app_user_model_id()")
    assert claimed < main.index("webview.create_window(")
    assert claimed < main.index("_open_windows_browser_fallback(")


def _icons() -> list[str]:
    text = ISS.read_text(encoding="utf-8")
    section = re.search(r"^\[Icons\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL).group(1)
    return [line.strip() for line in section.splitlines() if line.strip() and not line.strip().startswith(";")]


def test_the_launcher_shortcuts_carry_the_same_id() -> None:
    launchers = [line for line in _icons() if "vool.vbs" in line]
    assert len(launchers) == 2, launchers
    for line in launchers:
        assert f'AppUserModelID: "{wc.APP_USER_MODEL_ID}"' in line, line


# --- (b) the window icon ------------------------------------------------------------------------


def test_the_installed_layout_finds_the_icon_next_to_the_window_host(tmp_path) -> None:
    (tmp_path / "vool.ico").write_bytes(b"\x00\x00\x01\x00")
    assert wc.icon_path(here=tmp_path) == str(tmp_path / "vool.ico")


def test_a_source_checkout_finds_the_shipped_asset() -> None:
    found = wc.icon_path(here=ROOT / "installer" / "bundle")
    assert found and Path(found).resolve() == (ROOT / "installer" / "assets" / "vool.ico").resolve()


def test_no_icon_anywhere_is_none_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wc, "_ASSET_ICON", tmp_path / "missing" / "vool.ico")
    assert wc.icon_path(here=tmp_path) is None


def test_the_icon_is_the_one_the_installer_gives_the_desktop_shortcut() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert re.search(r'^Source:\s*"\{#SourcePath\}\\\.\.\\assets\\vool\.ico";\s*DestDir:\s*"\{app\}"',
                     text, re.MULTILINE), "vool.ico must be installed into {app}"
    desktop = [line for line in _icons() if line.startswith('Name: "{autodesktop}\\VOOL"')]
    assert desktop and 'IconFilename: "{app}\\vool.ico"' in desktop[0]
    assert wc.ICON_FILENAME == "vool.ico"
    assert (ROOT / "installer" / "assets" / wc.ICON_FILENAME).is_file()


def test_the_window_host_is_installed_next_to_the_icon() -> None:
    build = BUILD.read_text(encoding="utf-8")
    assert re.search(r'Copy-Item \(Join-Path \$PSScriptRoot "vool_window\.py"\) \(Join-Path \$Stage "vool_window\.py"\)', build)
    assert re.search(r'Source:\s*"\{#Stage\}\\vool_window\.py";\s*DestDir:\s*"\{app\}"', ISS.read_text(encoding="utf-8"))


def test_the_icon_is_handed_to_pywebview_on_windows_only() -> None:
    assert wc.start_icon_options(platform="darwin", here=ROOT / "installer" / "bundle") == {}
    assert wc.start_icon_options(platform="linux", here=ROOT / "installer" / "bundle") == {}
    options = wc.start_icon_options(platform="win32", here=ROOT / "installer" / "bundle")
    assert set(options) == {"icon"} and options["icon"].endswith("vool.ico")


def test_a_missing_icon_adds_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wc, "_ASSET_ICON", tmp_path / "missing.ico")
    assert wc.start_icon_options(platform="win32", here=tmp_path) == {}


def test_main_passes_the_icon_to_webview_start() -> None:
    from installer.bundle import vool_window as nw

    main = inspect.getsource(nw.main)
    assert "start_options.update(windows_chrome.start_icon_options())" in main
    assert main.index("start_options.update(windows_chrome.start_icon_options())") < main.index("webview.start(")


def test_icon_options_never_raise(monkeypatch) -> None:
    def boom(**_k):
        raise OSError("disk went away")

    monkeypatch.setattr(wc, "icon_path", boom)
    assert wc.start_icon_options(platform="win32") == {}
