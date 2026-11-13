"""Saving to a USB stick or a network share works, and still never replaces an existing file.

Bug hunt 0.7.1 (379e57f7), B17: the native saves (chat export, answer PDF, built files) put the
finished temporary file in place with ``os.link``. FAT32 and exFAT (USB sticks, SD cards) have no
hard links and many shares refuse them, so the save failed with a bare "OSError". Where the
destination has no hard links the same bytes must be written to an exclusively created file; an
existing file is never replaced, a half-written file is removed, and any other failure (a real
refusal) is reported unchanged. Save dialogs open in Downloads, not the profile folder.
"""
from __future__ import annotations

import errno
import os
import pathlib

import pytest

from installer.bundle import vool_window
from installer.bundle.vool_window import _WindowApi


def _no_hard_links(monkeypatch, exc: OSError) -> list:
    calls: list = []

    def link(src, dst, *a, **k):
        calls.append((src, dst))
        raise exc

    monkeypatch.setattr(vool_window.os, "link", link)
    return calls


def _staged(tmp_path: pathlib.Path, body: bytes) -> pathlib.Path:
    temporary = tmp_path / ".vool-export-tmp"
    temporary.write_bytes(body)
    return temporary


def _winerror(code: int) -> OSError:
    exc = OSError(errno.EINVAL, "Incorrect function")
    exc.winerror = code
    return exc


@pytest.mark.parametrize("exc", [
    OSError(errno.EPERM, "Operation not permitted"),        # vfat on Linux
    OSError(errno.EXDEV, "Invalid cross-device link"),
    OSError(errno.EOPNOTSUPP, "Operation not supported"),
    _winerror(1),                                            # ERROR_INVALID_FUNCTION: FAT32/exFAT
    _winerror(50),                                           # ERROR_NOT_SUPPORTED: a network share
], ids=["EPERM", "EXDEV", "EOPNOTSUPP", "winerror1", "winerror50"])
def test_a_destination_without_hard_links_gets_the_bytes(tmp_path, monkeypatch, exc) -> None:
    body = b"exported chat\n"
    destination = tmp_path / "usb" / "vool-chat.md"
    destination.parent.mkdir()
    calls = _no_hard_links(monkeypatch, exc)
    vool_window._deliver_no_clobber(_staged(tmp_path, body), destination, body)
    assert calls, "the atomic hard link is still tried first"
    assert destination.read_bytes() == body


def test_an_existing_file_is_never_replaced_by_the_fallback(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "vool-chat.md"
    destination.write_bytes(b"the person's own file")
    _no_hard_links(monkeypatch, _winerror(1))
    with pytest.raises(FileExistsError):
        vool_window._deliver_no_clobber(_staged(tmp_path, b"new"), destination, b"new")
    assert destination.read_bytes() == b"the person's own file"


def test_an_existing_file_still_stops_the_hard_link_path(tmp_path) -> None:
    destination = tmp_path / "vool-chat.md"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        vool_window._deliver_no_clobber(_staged(tmp_path, b"new"), destination, b"new")
    assert destination.read_bytes() == b"keep"


@pytest.mark.parametrize("exc", [
    PermissionError(errno.EACCES, "Access is denied"),
    OSError(errno.ENOSPC, "No space left on device"),
    _winerror(5),
], ids=["EACCES", "ENOSPC", "winerror5"])
def test_a_real_refusal_is_reported_not_worked_around(tmp_path, monkeypatch, exc) -> None:
    destination = tmp_path / "vool-chat.md"
    _no_hard_links(monkeypatch, exc)
    with pytest.raises(OSError) as raised:
        vool_window._deliver_no_clobber(_staged(tmp_path, b"x"), destination, b"x")
    assert raised.value is exc
    assert not destination.exists()


def test_a_half_written_fallback_file_is_removed(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "vool-chat.md"
    _no_hard_links(monkeypatch, _winerror(1))

    def fsync(_fd):
        raise OSError(errno.EIO, "the stick was pulled out")

    monkeypatch.setattr(vool_window.os, "fsync", fsync)
    with pytest.raises(OSError):
        vool_window._deliver_no_clobber(_staged(tmp_path, b"x"), destination, b"x")
    assert not destination.exists()


class _Headers:
    def __init__(self, content_type: str, disposition: str = "") -> None:
        self._content_type, self._disposition = content_type, disposition

    def get_content_type(self) -> str:
        return self._content_type

    def get(self, name: str, default: str = "") -> str:
        return self._disposition if name.lower() == "content-disposition" else default


class _Response:
    def __init__(self, body: bytes, headers: _Headers) -> None:
        self._body, self.headers = body, headers

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body


class _Window:
    def __init__(self, destination: pathlib.Path) -> None:
        self._destination = destination
        self.dialogs: list[dict] = []

    def create_file_dialog(self, *args: object, **kwargs: object):
        self.dialogs.append(kwargs)
        return (str(self._destination),)


class _Webview:
    SAVE_DIALOG = 1


def _serve(monkeypatch, response: _Response) -> None:
    opener = type("O", (), {"open": lambda self, url, timeout=0: response})()
    monkeypatch.setattr(vool_window.urllib.request, "build_opener", lambda *a, **k: opener)


def _usb_api(tmp_path, monkeypatch, name: str):
    stick = tmp_path / "E-drive"
    stick.mkdir()
    destination = stick / name
    window = _Window(destination)
    api = _WindowApi()
    api.set_window(window, _Webview())
    _no_hard_links(monkeypatch, _winerror(1))
    return api, window, destination, stick


def test_a_chat_export_to_a_usb_stick_is_saved(tmp_path, monkeypatch) -> None:
    body = b"# chat\n"
    api, _window, destination, stick = _usb_api(tmp_path, monkeypatch, "vool-chat.md")
    _serve(monkeypatch, _Response(body, _Headers("text/markdown")))
    assert api.save_chat_export("session-1", "md") == {"ok": True}
    assert destination.read_bytes() == body
    assert sorted(p.name for p in stick.iterdir()) == ["vool-chat.md"], "the temporary file was left behind"


def test_a_built_file_to_a_usb_stick_is_saved(tmp_path, monkeypatch) -> None:
    body = b"deliverable bytes"
    api, _window, destination, stick = _usb_api(tmp_path, monkeypatch, "report.txt")
    _serve(monkeypatch, _Response(body, _Headers("text/plain", 'attachment; filename="report.txt"')))
    assert api.save_deliverable("session-1", "dlv_" + "0" * 32) == {"ok": True}
    assert destination.read_bytes() == body
    assert sorted(p.name for p in stick.iterdir()) == ["report.txt"]


def test_save_dialogs_open_in_downloads(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(vool_window.Path, "home", classmethod(lambda cls: home))
    api, window, _destination, _stick = _usb_api(tmp_path, monkeypatch, "vool-chat.md")
    _serve(monkeypatch, _Response(b"# chat\n", _Headers("text/markdown")))
    api.save_chat_export("session-1", "md")
    _serve(monkeypatch, _Response(b"x", _Headers("text/plain", 'attachment; filename="report.txt"')))
    window._destination = window._destination.with_name("report.txt")
    api.save_deliverable("session-1", "dlv_" + "0" * 32)
    assert [d.get("directory") for d in window.dialogs] == [str(home / "Downloads")] * 2


def test_without_a_downloads_folder_the_dialog_keeps_its_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vool_window.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert vool_window._save_dialog_start_folder() == ""
