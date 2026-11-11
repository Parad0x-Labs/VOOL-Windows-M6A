"""B3 and B17: native saves for chat bundles, and saves to drives that cannot hard-link.

B3: the .voolsession export asked the page to download the bundle, which the desktop window
cancels, and still said "Saved". The bridge now saves it through a native dialog.
B17: every native save hard-linked a temporary file into place; FAT/exFAT USB sticks and many
network shares refuse hard links, so the save failed. It now falls back to an exclusive create.
"""
from __future__ import annotations

import errno
import io
import os
import pathlib
import urllib.error

import pytest

from core.vool_chat_page import render_vool_chat_html
from core.vool_settings_page import render_vool_settings_html
from installer.bundle import vool_window
from installer.bundle.vool_window import _WindowApi

_BUNDLE_PATH = "/home/u/.vool/session_bundles/chat-0123456789abcdef-20261111T000000Z.voolsession"


class _Headers:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type

    def get(self, name: str, default: str = "") -> str:
        return default


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self._body = body
        self.headers = _Headers(content_type)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body


class _Opener:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.urls: list[str] = []

    def open(self, url: str, timeout: float = 0) -> _Response:
        self.urls.append(url)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


class _Webview:
    SAVE_DIALOG = 1


class _Window:
    def __init__(self, destination: pathlib.Path | None) -> None:
        self._destination = destination
        self.calls: list[dict] = []

    def create_file_dialog(self, *args: object, **kwargs: object):
        self.calls.append(dict(kwargs))
        return (str(self._destination),) if self._destination is not None else None


def _api(window: _Window) -> _WindowApi:
    api = _WindowApi()
    api.set_window(window, _Webview())
    return api


def _serve(monkeypatch: pytest.MonkeyPatch, outcome: object) -> _Opener:
    opener = _Opener(outcome)
    monkeypatch.setattr(vool_window.urllib.request, "build_opener", lambda *a, **k: opener)
    return opener


# --- B3: the bundle bridge -------------------------------------------------------------------

def test_a_bundle_is_saved_where_the_user_picks(tmp_path, monkeypatch) -> None:
    body = b"VOOLSESSION\x00signed bytes"
    destination = tmp_path / "my-chat.voolsession"
    opener = _serve(monkeypatch, _Response(body))
    window = _Window(destination)
    result = _api(window).save_session_bundle(_BUNDLE_PATH, "chat:abc/1")
    assert result == {"ok": True}
    assert destination.read_bytes() == body
    assert [p.name for p in tmp_path.iterdir()] == ["my-chat.voolsession"]
    # The page names only the bundle; the confined download route serves it.
    assert len(opener.urls) == 1 and "/api/session/bundle/download?path=" in opener.urls[0]
    assert window.calls[0]["save_filename"] == "chat_abc_1.voolsession"
    assert window.calls[0]["file_types"] == ("VOOL chat bundles (*.voolsession)",)


def test_a_cancelled_dialog_saves_nothing(tmp_path, monkeypatch) -> None:
    _serve(monkeypatch, _Response(b"bytes"))
    assert _api(_Window(None)).save_session_bundle(_BUNDLE_PATH, "s") == {"ok": False, "cancelled": True}
    assert list(tmp_path.iterdir()) == []


def test_an_existing_file_is_never_overwritten(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "chat.voolsession"
    destination.write_bytes(b"keep me")
    _serve(monkeypatch, _Response(b"new bytes"))
    result = _api(_Window(destination)).save_session_bundle(_BUNDLE_PATH, "s")
    assert result["ok"] is False and result["error"] == "destination_exists"
    assert destination.read_bytes() == b"keep me"
    assert [p.name for p in tmp_path.iterdir()] == ["chat.voolsession"]


@pytest.mark.parametrize("path", ["", "C:/Users/x/secret.txt", "a" * 5000 + ".voolsession", 7])
def test_only_a_bundle_path_is_accepted(path, monkeypatch) -> None:
    opener = _serve(monkeypatch, _Response(b"x"))
    result = _api(_Window(None)).save_session_bundle(path, "s")
    assert result == {"ok": False, "error": "invalid_bundle_identity"}
    assert opener.urls == []


def test_a_refused_download_is_reported_and_saves_nothing(tmp_path, monkeypatch) -> None:
    refusal = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"ok": false, "error": "path_not_allowed"}'))
    _serve(monkeypatch, refusal)
    result = _api(_Window(tmp_path / "x.voolsession")).save_session_bundle(_BUNDLE_PATH, "s")
    assert result == {"ok": False, "error": "bundle_unavailable", "message": "path_not_allowed"}
    assert list(tmp_path.iterdir()) == []


def test_a_non_bundle_response_is_refused(tmp_path, monkeypatch) -> None:
    _serve(monkeypatch, _Response(b"<html>", "text/html"))
    result = _api(_Window(tmp_path / "x.voolsession")).save_session_bundle(_BUNDLE_PATH, "s")
    assert result == {"ok": False, "error": "invalid_bundle_response"}
    assert list(tmp_path.iterdir()) == []


def _export_handler(html: str, marker: str) -> str:
    start = html.index(marker)
    return html[start:start + 4000]


@pytest.mark.parametrize("render,marker", [
    (render_vool_settings_html, "exportBtn.addEventListener('click'"),
    (render_vool_chat_html, "sbExportBtn.addEventListener('click'"),
])
def test_both_export_buttons_save_through_the_bridge_inside_the_app(render, marker) -> None:
    handler = _export_handler(render(), marker)
    native = handler.index("save_session_bundle(")
    fallback = handler.index("a.download")
    saved = handler.index("'Saved: signed")
    # Inside the app the bridge saves; the download link is only the plain-browser branch, and a
    # cancelled or failed save returns before the "Saved" message.
    assert native < fallback < saved
    assert "Not saved: the save was cancelled." in handler
    assert "pywebview.api" in handler


# --- B17: drives that cannot hard-link ---------------------------------------------------------

def _refuse_links(monkeypatch: pytest.MonkeyPatch, code: int = errno.EPERM) -> list[tuple]:
    calls: list[tuple] = []

    def no_link(src, dst, *a, **k):
        calls.append((src, dst))
        raise OSError(code, "hard links are not supported on this volume")

    monkeypatch.setattr(vool_window.os, "link", no_link)
    return calls


@pytest.mark.parametrize("save", ["bundle", "chat", "deliverable"])
def test_every_native_save_works_on_a_drive_without_hard_links(save, tmp_path, monkeypatch) -> None:
    links = _refuse_links(monkeypatch)
    body = b"exported bytes\n"
    if save == "bundle":
        destination = tmp_path / "c.voolsession"
        _serve(monkeypatch, _Response(body))
        result = _api(_Window(destination)).save_session_bundle(_BUNDLE_PATH, "s")
    elif save == "chat":
        destination = tmp_path / "c.md"
        _serve(monkeypatch, _Response(body, "text/markdown"))
        result = _api(_Window(destination)).save_chat_export("session-1", "md")
    else:
        destination = tmp_path / "report.txt"
        _serve(monkeypatch, _Response(body, "text/plain"))
        result = _api(_Window(destination)).save_deliverable("session-1", "dlv_" + "0" * 32)
    assert result == {"ok": True}
    assert links, "the hard link is still tried first"
    assert destination.read_bytes() == body
    assert [p.name for p in tmp_path.iterdir()] == [destination.name]


def test_without_hard_links_an_existing_file_is_still_never_overwritten(tmp_path, monkeypatch) -> None:
    _refuse_links(monkeypatch)
    destination = tmp_path / "c.voolsession"
    destination.write_bytes(b"keep me")
    _serve(monkeypatch, _Response(b"new"))
    result = _api(_Window(destination)).save_session_bundle(_BUNDLE_PATH, "s")
    assert result["error"] == "destination_exists"
    assert destination.read_bytes() == b"keep me"
    assert [p.name for p in tmp_path.iterdir()] == ["c.voolsession"]


def test_a_direct_write_that_fails_leaves_no_partial_file(tmp_path, monkeypatch) -> None:
    _refuse_links(monkeypatch)
    destination = tmp_path / "c.voolsession"
    real_fsync = os.fsync
    calls = {"n": 0}

    def failing_fsync(fd):
        calls["n"] += 1
        if calls["n"] >= 2:  # the temporary file syncs; the direct write then fails (drive pulled)
            raise OSError(errno.EIO, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(vool_window.os, "fsync", failing_fsync)
    _serve(monkeypatch, _Response(b"bytes"))
    result = _api(_Window(destination)).save_session_bundle(_BUNDLE_PATH, "s")
    assert result == {"ok": False, "error": "OSError"}
    assert list(tmp_path.iterdir()) == []


def test_windows_save_dialogs_open_in_downloads(tmp_path, monkeypatch) -> None:
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    monkeypatch.setattr(vool_window.Path, "home", classmethod(lambda cls: tmp_path))
    assert vool_window._save_dialog_directory() == str(tmp_path / "Downloads")
    _serve(monkeypatch, _Response(b"# chat\n", "text/markdown"))
    window = _Window(None)
    _api(window).save_chat_export("session-1", "md")
    assert window.calls[0]["directory"] == str(tmp_path / "Downloads")


def test_save_dialogs_keep_their_default_folder_off_windows_or_without_downloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vool_window.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(vool_window.sys, "platform", "darwin")
    (tmp_path / "Downloads").mkdir()
    assert vool_window._save_dialog_directory() == ""
    (tmp_path / "Downloads").rmdir()
    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    assert vool_window._save_dialog_directory() == ""
