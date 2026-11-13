"""Every window VOOL opens gets the Windows chrome (dark title bar, VOOL's icon); only the pet not.

The owner wants the dark title bar with white caption buttons and VOOL's logo on EVERY VOOL
window, including ones added later. So there is one place that opens a VOOL window,
``vool_window._create_vool_window``, and it styles what it opens. These rows scan the product
source and fail if any ``create_window`` call bypasses that helper, other than the frameless pet
(which has no title bar and its own policy in pet_native_windows).

Excluded from the scan: tests, and tools/pet_native_proof.py, a developer measurement harness that
photographs the pet over a known backdrop window and must not have its pixels changed by chrome.
"""
from __future__ import annotations

import ast
import subprocess
import types
from pathlib import Path

import pytest

from installer.bundle import windows_chrome as wc

ROOT = Path(__file__).resolve().parent.parent
HELPER = "_create_vool_window"
PET = "_WindowApi.detach_companion"
NOT_PRODUCT = {"tools/pet_native_proof.py"}


def _product_sources() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / rel for rel in listed.stdout.split()
            if not rel.startswith("tests/") and rel not in NOT_PRODUCT]


def _create_window_calls(path: Path) -> list[tuple[str, ast.Call]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, ast.Call]] = []

    def walk(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, [*scope, child.name])
                continue
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "create_window"):
                found.append((".".join(scope), child))
            walk(child, scope)

    walk(tree, [])
    return found


def test_no_window_is_opened_outside_the_helper_except_the_pet() -> None:
    offenders = []
    seen_helper = seen_pet = False
    for path in _product_sources():
        for scope, call in _create_window_calls(path):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "installer/bundle/vool_window.py" and scope == HELPER:
                seen_helper = True
                continue
            if rel == "installer/bundle/vool_window.py" and scope == PET:
                flags = [kw for kw in call.keywords if kw.arg is None]
                assert flags and ast.unparse(flags[0].value) == "COMPANION_WINDOW_FLAGS", ast.unparse(call)
                seen_pet = True
                continue
            offenders.append(f"{rel}:{call.lineno} in {scope or '<module>'}: {ast.unparse(call)[:80]}")
    assert not offenders, "open VOOL windows through _create_vool_window:\n" + "\n".join(offenders)
    assert seen_helper and seen_pet


def test_main_settings_and_setup_go_through_the_helper() -> None:
    tree = ast.parse((ROOT / "installer" / "bundle" / "vool_window.py").read_text(encoding="utf-8"))
    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == HELPER:
                    callers.add(node.name)
    assert {"main", "open_settings", "open_setup"} <= callers, callers
    assert "detach_companion" not in callers


# --- the helper ---------------------------------------------------------------------------------


@pytest.fixture
def nw(monkeypatch):
    from installer.bundle import vool_window

    return vool_window


def test_the_helper_opens_then_styles_the_window(nw, monkeypatch) -> None:
    styled: list = []
    monkeypatch.setattr(wc, "style_native_window", lambda window, **_k: styled.append(window) or True)
    made: list = []
    module = types.SimpleNamespace(create_window=lambda *a, **k: made.append((a, k)) or "WINDOW")
    assert nw._create_vool_window(module, "VOOL Something", "http://x", width=1) == "WINDOW"
    assert made == [(("VOOL Something", "http://x"), {"width": 1, "background_color": nw.WINDOW_BACKGROUND})]
    assert styled == ["WINDOW"]


def test_no_window_means_nothing_to_style(nw, monkeypatch) -> None:
    styled: list = []
    monkeypatch.setattr(wc, "style_native_window", lambda window, **_k: styled.append(window) or True)
    assert nw._create_vool_window(types.SimpleNamespace(create_window=lambda *a, **k: None), "t") is None
    assert styled == []


def test_a_styling_failure_never_costs_the_window(nw, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("chrome went away")

    monkeypatch.setattr(wc, "style_native_window", boom)
    assert nw._create_vool_window(types.SimpleNamespace(create_window=lambda *a, **k: "W"), "t") == "W"


# --- the styler sets VOOL's icon on the window too ----------------------------------------------


class _User32:
    def __init__(self, handle=0x5150, raises=False) -> None:
        self.loads: list = []
        self.sent: list = []
        self.handle = handle
        self.raises = raises

    def GetSystemMetrics(self, index):
        return {11: 32, 12: 32, 49: 16, 50: 16}[index]

    def LoadImageW(self, instance, name, kind, cx, cy, flags):
        if self.raises:
            raise OSError("user32 went away")
        self.loads.append((instance, getattr(name, "value", name), kind, cx, cy, flags))
        return self.handle

    def SendMessageW(self, hwnd, message, wparam, lparam):
        self.sent.append((hwnd, message, wparam, lparam))
        return 0


def test_the_icon_is_set_small_and_big_from_the_shipped_ico() -> None:
    assert (wc.WM_SETICON, wc.ICON_SMALL, wc.ICON_BIG) == (0x0080, 0, 1)
    assert (wc.IMAGE_ICON, wc.LR_LOADFROMFILE) == (1, 0x0010)
    user32 = _User32()
    assert wc.set_window_icon(0x77, "C:\\VOOL\\vool.ico", user32=user32) is True
    assert {load[3:5] for load in user32.loads} == {(16, 16), (32, 32)}
    assert all(load[1] == "C:\\VOOL\\vool.ico" and load[2] == 1 and load[5] & 0x10 for load in user32.loads)
    assert sorted(sent[2] for sent in user32.sent) == [0, 1]
    assert all(sent[:2] == (0x77, 0x0080) and sent[3] == 0x5150 for sent in user32.sent)


def test_the_icon_is_a_silent_no_op_on_failure() -> None:
    assert wc.set_window_icon(0x77, None, user32=_User32()) is False
    assert wc.set_window_icon(0, "C:\\VOOL\\vool.ico", user32=_User32()) is False
    assert wc.set_window_icon(0x77, "C:\\VOOL\\vool.ico", user32=_User32(handle=0)) is False
    assert wc.set_window_icon(0x77, "C:\\VOOL\\vool.ico", user32=_User32(raises=True)) is False


def test_the_styler_applies_dark_mode_and_the_icon_together(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(wc, "apply_dark_title_bar", lambda hwnd, **_k: calls.append(("dark", hwnd)) or True)
    monkeypatch.setattr(wc, "set_window_icon", lambda hwnd, path=None, **_k: calls.append(("icon", hwnd)) or True)
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x42)))
    assert wc.style_native_window(window, platform="win32", background=False) is True
    assert calls == [("dark", 0x42), ("icon", 0x42)]
    calls.clear()
    assert wc.style_native_window(window, platform="darwin", background=False) is False
    assert calls == []


def test_a_dark_mode_failure_still_sets_the_icon(monkeypatch) -> None:
    calls: list = []

    def dark(_hwnd, **_k):
        raise OSError("no dwm")

    monkeypatch.setattr(wc, "apply_dark_title_bar", dark)
    monkeypatch.setattr(wc, "set_window_icon", lambda hwnd, path=None, **_k: calls.append(hwnd) or True)
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x42)))
    assert wc.style_native_window(window, platform="win32", background=False) is True
    assert calls == [0x42]
