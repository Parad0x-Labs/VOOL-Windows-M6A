"""A browser session's downloads land in the session's own folder, never the user's Downloads.

With `accept_downloads=True` and no downloads path, Chromium is left with its default download
folder: on Windows runs the user's real ~/Downloads changed while a session downloaded a file
(Steve jobs 48d and 48e). The engine is now told the session's own disposable folder.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from core.vool_browser import sessions


class _Stop(Exception):
    pass


def test_the_engine_is_given_a_downloads_folder_inside_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOOL_BROWSER_SCRATCH_ROOT", str(tmp_path))
    seen: dict = {}

    class _Chromium:
        def launch_persistent_context(self, **kwargs):
            seen.update(kwargs)
            raise _Stop("launch recorded")

    class _Driver:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _Driver()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    handle = sessions.SessionHandle(session="dl", engine_binary="engine.exe")
    handle._thread.join(timeout=10)

    assert seen, "the engine was never launched"
    downloads = Path(seen.get("downloads_path") or "")
    assert seen.get("downloads_path"), "no downloads folder was given, so Chromium uses the user's own"
    assert downloads.is_relative_to(handle.dir), (downloads, handle.dir)
    assert downloads.is_dir()


def test_the_fresh_profile_names_the_session_folder_as_its_download_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Edge creates a probe `.tmp` file in its default download folder at startup, before Playwright's
    # downloads path applies (Steve job 48g), so the profile itself must already name the session folder.
    import json

    monkeypatch.setenv("VOOL_BROWSER_SCRATCH_ROOT", str(tmp_path))
    seen: dict = {}

    class _Chromium:
        def launch_persistent_context(self, **kwargs):
            prefs = Path(kwargs["user_data_dir"]) / "Default" / "Preferences"
            seen["prefs"] = json.loads(prefs.read_text(encoding="utf-8")) if prefs.is_file() else None
            raise _Stop("launch recorded")

    class _Driver:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _Driver()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    handle = sessions.SessionHandle(session="dlp", engine_binary="engine.exe")
    handle._thread.join(timeout=10)

    prefs = seen.get("prefs")
    assert prefs, "the profile had no Preferences naming a download folder before launch"
    assert Path(prefs["download"]["default_directory"]) == handle.downloads_dir
    assert Path(prefs["savefile"]["default_directory"]) == handle.downloads_dir
    assert prefs["download"]["prompt_for_download"] is False
