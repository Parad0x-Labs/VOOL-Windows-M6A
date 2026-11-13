"""Claims sweep on Steve (170f6572), window lane.

* A command run straight with the bundled Python (the README's ``python -m core.honesty_receipt
  demo``) made a second VOOL home in ``<install>\\app\\.vool_local``: the Windows bundle writes no
  install receipt, so home resolution fell back to the source-checkout default. An installed
  Windows bundle now resolves to ``native_runtime_home()``.
* Marking ``C:\\`` came back as "cancelled" and Settings showed nothing: pywebview's picker returns
  an empty tuple for a drive root, and an empty result was read as Cancel. It is now a refusal with
  a reason; Cancel (None) is unchanged.
* ``vool://`` links did nothing visible: the handler is the process Windows lets take the
  foreground, and it never handed that on to VOOL further down the launch chain.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from core import runtime_paths
from installer.bundle import vool_protocol_handler as handler
from installer.bundle import vool_window

# --- the bundled Python finds the real home ------------------------------------------------------

def _bundle(tmp_path: Path, *, schema: str = "vool.bundle_manifest.v1", python: bool = True) -> Path:
    install = tmp_path / "Programs" / "VOOL"
    (install / "app" / "core").mkdir(parents=True)
    if python:
        (install / "python").mkdir()
        (install / "python" / "python.exe").write_bytes(b"MZ")
    # PowerShell writes the manifest with a UTF-8 BOM.
    (install / "bundle_manifest.json").write_text(json.dumps({"schema": schema}), encoding="utf-8-sig")
    return install / "app"


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(runtime_paths.sys, "platform", "win32")


def test_an_installed_bundle_resolves_to_the_native_home(tmp_path, on_windows) -> None:
    app = _bundle(tmp_path)
    local = tmp_path / "LocalAppData"
    env = {"LOCALAPPDATA": str(local)}
    assert runtime_paths.discover_installed_runtime_home(project_root=app, env=env) == (local / "VOOL").resolve()


def test_active_home_from_the_bundled_python_is_not_the_program_folder(tmp_path, monkeypatch, on_windows) -> None:
    app = _bundle(tmp_path)
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(runtime_paths, "PROJECT_ROOT", app)
    home = runtime_paths.active_vool_home({"VOOL_HOME": "", "LOCALAPPDATA": str(local)})
    assert home == (local / "VOOL").resolve()
    assert app not in home.parents and home != (app / ".vool_local").resolve()


def test_an_explicit_vool_home_still_wins(tmp_path, monkeypatch, on_windows) -> None:
    app = _bundle(tmp_path)
    monkeypatch.setattr(runtime_paths, "PROJECT_ROOT", app)
    chosen = tmp_path / "chosen-home"
    assert runtime_paths.active_vool_home({"VOOL_HOME": str(chosen), "LOCALAPPDATA": str(tmp_path)}) == chosen.resolve()


def test_an_install_receipt_still_wins(tmp_path, on_windows) -> None:
    app = _bundle(tmp_path)
    receipt_home = tmp_path / "receipt-home"
    (app / "install_receipt.json").write_text(json.dumps({"runtime_home": str(receipt_home)}), encoding="utf-8")
    assert runtime_paths.discover_installed_runtime_home(project_root=app, env={"LOCALAPPDATA": str(tmp_path)}) == receipt_home.resolve()


@pytest.mark.parametrize("layout", ["no-manifest", "wrong-schema", "no-python", "not-app-folder"])
def test_anything_that_is_not_an_installed_bundle_keeps_the_old_answer(tmp_path, on_windows, layout) -> None:
    if layout == "no-manifest":
        root = tmp_path / "checkout" / "app"
        (root / "core").mkdir(parents=True)
    elif layout == "wrong-schema":
        root = _bundle(tmp_path, schema="something.else")
    elif layout == "no-python":
        root = _bundle(tmp_path, python=False)
    else:
        root = _bundle(tmp_path).parent   # the install folder itself, not its app folder
    assert runtime_paths.discover_installed_runtime_home(project_root=root, env={"LOCALAPPDATA": str(tmp_path)}) is None


def test_other_platforms_are_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    assert runtime_paths.discover_installed_runtime_home(project_root=_bundle(tmp_path)) is None


# --- marking a drive root says why ---------------------------------------------------------------

class _Picker:
    def __init__(self, result) -> None:
        self._result = result

    def create_file_dialog(self, *_a, **_k):
        return self._result


@pytest.fixture
def picker(monkeypatch):
    fake = types.ModuleType("webview")
    fake.FOLDER_DIALOG = "FOLDER_DIALOG"
    monkeypatch.setitem(sys.modules, "webview", fake)

    def make(result):
        api = vool_window._WindowApi()
        api.set_window(_Picker(result), fake)
        return api
    return make


@pytest.mark.parametrize("result", [(), [], ("",), ("   ",), ""], ids=["empty-tuple", "empty-list", "blank", "spaces", "empty-str"])
def test_ok_with_no_folder_is_a_refusal_with_a_reason(picker, result) -> None:
    out = picker(result).mark_disposable_folder("settings")
    assert out == {"ok": False, "refused": vool_window._NO_FOLDER_RETURNED}
    assert "whole drive" in out["refused"] and "folder inside it" in out["refused"]


def test_cancel_is_still_a_quiet_cancel(picker) -> None:
    assert picker(None).mark_disposable_folder("settings") == {"ok": False, "cancelled": True}


@pytest.mark.parametrize("page", ["core/vool_settings_page.py", "core/vool_setup_page.py"])
def test_both_pages_show_a_refusal(page) -> None:
    source = (Path(__file__).resolve().parents[1] / page).read_text(encoding="utf-8")
    assert "res.refused" in source


# --- vool:// hands the foreground on --------------------------------------------------------------

def test_starting_vool_passes_on_the_foreground_first(monkeypatch, tmp_path) -> None:
    (tmp_path / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    order: list[str] = []
    monkeypatch.setattr(handler, "_pass_on_the_foreground", lambda: order.append("allow") or True)
    monkeypatch.setattr(handler.subprocess, "Popen", lambda *a, **k: order.append("popen"))
    handler._start_bundle(tmp_path)
    assert order == ["allow", "popen"]


def test_a_refused_launcher_path_starts_nothing_and_allows_nothing(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "100%"
    bad.mkdir()
    (bad / "VOOL.cmd").write_text("@echo off\n", encoding="utf-8")
    order: list[str] = []
    monkeypatch.setattr(handler, "_pass_on_the_foreground", lambda: order.append("allow") or True)
    monkeypatch.setattr(handler.subprocess, "Popen", lambda *a, **k: order.append("popen"))
    with pytest.raises(ValueError):
        handler._start_bundle(bad)
    assert order == []


def test_the_foreground_right_goes_to_asfw_any_on_windows(monkeypatch) -> None:
    import ctypes

    calls: list[int] = []

    class _Allow:
        def __call__(self, value):
            calls.append(value)
            return 1

    monkeypatch.setattr(handler, "os", types.SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=types.SimpleNamespace(AllowSetForegroundWindow=_Allow())), raising=False)
    assert handler._pass_on_the_foreground() is True
    assert calls == [0xFFFFFFFF]


def test_a_failing_foreground_call_never_fails_the_link(monkeypatch) -> None:
    import ctypes

    class _Boom:
        def __call__(self, value):
            raise OSError("access denied")

    monkeypatch.setattr(handler, "os", types.SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=types.SimpleNamespace(AllowSetForegroundWindow=_Boom())), raising=False)
    assert handler._pass_on_the_foreground() is False


def test_other_platforms_skip_the_foreground_call(monkeypatch) -> None:
    monkeypatch.setattr(handler, "os", types.SimpleNamespace(name="posix", environ={}))
    assert handler._pass_on_the_foreground() is False
