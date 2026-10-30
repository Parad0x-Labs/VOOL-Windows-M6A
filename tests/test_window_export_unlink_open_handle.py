"""Native saves must close the temporary file before unlinking it.

Windows refuses to unlink a file that still has an open handle (WinError 32). Linux allows it, so
these tests model the Windows sharing rule: every temporary file the module creates is tracked, and
``Path.unlink`` on a path whose handle is still open raises ``PermissionError``.
"""
from __future__ import annotations

import errno
import pathlib
import tempfile

import pytest

from installer.bundle import vool_window
from installer.bundle.vool_window import _WindowApi

_DELIVERABLE_ID = "dlv_" + "0" * 32


class _Headers:
    def __init__(self, content_type: str, disposition: str = "") -> None:
        self._content_type = content_type
        self._disposition = disposition

    def get_content_type(self) -> str:
        return self._content_type

    def get(self, name: str, default: str = "") -> str:
        return self._disposition if name.lower() == "content-disposition" else default


class _Response:
    def __init__(self, body: bytes, headers: _Headers) -> None:
        self._body = body
        self.headers = headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body


class _Opener:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def open(self, url: str, timeout: float = 0) -> _Response:
        return self._response


class _Webview:
    SAVE_DIALOG = 1


class _Window:
    def __init__(self, destination: pathlib.Path) -> None:
        self._destination = destination

    def create_file_dialog(self, *args: object, **kwargs: object) -> tuple[str, ...]:
        return (str(self._destination),)


@pytest.fixture
def windows_sharing_rule(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    """Track temp files; unlinking one whose handle is open fails like WinError 32."""
    real_factory = tempfile.NamedTemporaryFile
    real_unlink = pathlib.Path.unlink
    tracked: list[tuple[pathlib.Path, object]] = []
    created: list[pathlib.Path] = []

    def factory(*args: object, **kwargs: object):
        handle = real_factory(*args, **kwargs)
        path = pathlib.Path(handle.name)
        tracked.append((path, handle))
        created.append(path)
        return handle

    def unlink(self: pathlib.Path, missing_ok: bool = False) -> None:
        for path, handle in tracked:
            if path == self and not handle.closed:
                raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by another process", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(vool_window.tempfile, "NamedTemporaryFile", factory)
    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    yield created
    for _, handle in tracked:
        handle.close()


def _api(destination: pathlib.Path) -> _WindowApi:
    api = _WindowApi()
    api.set_window(_Window(destination), _Webview())
    return api


def _serve(monkeypatch: pytest.MonkeyPatch, response: _Response) -> None:
    monkeypatch.setattr(vool_window.urllib.request, "build_opener", lambda *a, **k: _Opener(response))


def _assert_saved(destination: pathlib.Path, created: list[pathlib.Path], body: bytes, result: dict) -> None:
    assert result == {"ok": True}
    assert destination.read_bytes() == body
    assert created, "the save must stage through a temporary file"
    assert not any(path.exists() for path in created)
    assert [p for p in destination.parent.iterdir() if p != destination] == []


def test_chat_export_closes_temporary_before_unlink(tmp_path, monkeypatch, windows_sharing_rule) -> None:
    body = b"# chat\n"
    destination = tmp_path / "vool-chat.md"
    _serve(monkeypatch, _Response(body, _Headers("text/markdown")))
    result = _api(destination).save_chat_export("session-1", "md")
    _assert_saved(destination, windows_sharing_rule, body, result)


def test_deliverable_closes_temporary_before_unlink(tmp_path, monkeypatch, windows_sharing_rule) -> None:
    body = b"deliverable bytes"
    destination = tmp_path / "report.txt"
    _serve(monkeypatch, _Response(body, _Headers("text/plain", 'attachment; filename="report.txt"')))
    result = _api(destination).save_deliverable("session-1", _DELIVERABLE_ID)
    _assert_saved(destination, windows_sharing_rule, body, result)
