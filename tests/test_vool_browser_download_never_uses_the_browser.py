"""A session download is fetched through the session's request context, never by the browser's download machinery.

Edge on Windows writes a browser-handled download into the user's real Downloads folder before
Playwright moves it, whatever download folder it is told (Steve jobs 48g and 48h: a 64 KiB `.tmp`
beside the user's files). So `op_download` fetches the bytes itself through `context.request`,
judges every redirect hop by the same origin and grant rule as the first URL, and stops reading
past `max_bytes`: one that names no length, names too much or sends more than it named is refused.
The engine is launched with downloads refused, so a page cannot start one either.
"""
from __future__ import annotations

import http.client
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from core.vool_browser import ops, sessions
from core.vool_browser.sessions import OpFailed

BODY = b"A" * 65536


def _server(routes: dict[str, Any]) -> tuple[ThreadingHTTPServer, str, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            seen.append({"path": self.path, "cookie": self.headers.get("Cookie", "")})
            routes.get(self.path, lambda h: h.send_error(404))(self)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


def _send(handler: BaseHTTPRequestHandler, body: bytes, *, length: int | None = None) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    if length is not None:
        handler.send_header("Content-Length", str(length))
    handler.end_headers()
    handler.wfile.write(body)


def _redirect(location: str):
    def answer(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    return answer


def test_a_same_origin_redirect_hop_is_fetched_by_vool_not_followed_by_the_driver(sites) -> None:
    base, _, handle = sites
    context = _Context()
    _download(handle, base + "/same", context=context)
    assert [call["url"] for call in context.request.calls] == [base + "/same", base + "/file"]


class _NoBrowser:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the browser page was used for a download ({name})")


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self._body = status, headers, body

    def body(self) -> bytes:
        return self._body

    def dispose(self) -> None:
        return None


class _Request:
    """Stands in for Playwright's `context.request`: the session's cookie jar rides every call, and
    `max_redirects=0` hands a redirect back instead of following it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        import urllib.error
        import urllib.request

        self.calls.append({"url": url, **kwargs})
        assert kwargs.get("max_redirects") == 0, "redirects must come back to VOOL, one hop at a time"

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(url, headers={"Cookie": "sid=session-cookie"})
        try:
            with opener.open(request, timeout=10) as response:
                try:
                    body = response.read()
                except http.client.IncompleteRead as short:
                    body = short.partial
                return _Response(response.status, dict(response.headers), body)
        except urllib.error.HTTPError as exc:
            return _Response(exc.code, dict(exc.headers or {}), b"")


class _Context:
    def __init__(self) -> None:
        self.request = _Request()

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the browser context was used for a download ({name})")


class _Grants:
    def allows(self, origin: str, kind: str) -> bool:
        return False


@pytest.fixture()
def sites(tmp_path: Path):
    other, other_base, _ = _server({"/file": lambda h: _send(h, BODY, length=len(BODY))})
    routes: dict[str, Any] = {}
    main, base, seen = _server(routes)
    routes.update({
        "/file": lambda h: _send(h, BODY, length=len(BODY)),
        "/same": _redirect("/file"),
        "/cross": _redirect(f"{other_base}/file"),
        "/lying": lambda h: _send(h, b"B" * (3 * 1024 * 1024), length=1000),
        "/unsized": lambda h: _send(h, b"C" * (3 * 1024 * 1024)),
        "/declared-huge": lambda h: _send(h, b"D" * 1024, length=50 * 1024 * 1024),
    })
    handle = types.SimpleNamespace(
        primary_origin=base, current_url=base + "/", grants=_Grants(), staging_dir=tmp_path / "staging",
        spend_download=lambda size: None)
    handle.staging_dir.mkdir()
    yield base, seen, handle
    main.shutdown()
    other.shutdown()


def _download(handle: Any, url: str, max_bytes: int = 200000, context: _Context | None = None) -> dict:
    view = {"page": _NoBrowser(), "context": context or _Context()}
    return ops.op_download(handle, view, {"url": url, "max_bytes": max_bytes})


def test_a_download_is_fetched_through_the_session_request_context_and_no_browser_download(sites) -> None:
    base, seen, handle = sites
    context = _Context()
    receipt = _download(handle, base + "/file", context=context)
    assert [call["url"] for call in context.request.calls] == [base + "/file"]
    assert receipt["bytes"] == len(BODY)
    assert Path(receipt["path"]).read_bytes() == BODY
    assert Path(receipt["path"]).parent == handle.staging_dir
    assert seen[-1]["cookie"] == "sid=session-cookie"


def test_a_same_origin_redirect_is_followed(sites) -> None:
    base, _, handle = sites
    assert _download(handle, base + "/same")["bytes"] == len(BODY)


def test_a_redirect_to_an_origin_without_a_grant_is_refused_and_leaves_nothing(sites) -> None:
    base, _, handle = sites
    with pytest.raises(OpFailed) as refused:
        _download(handle, base + "/cross")
    assert refused.value.status == "cross_origin_refused"
    assert list(handle.staging_dir.iterdir()) == []


@pytest.mark.parametrize(("path", "status"), [("/unsized", "download_unsized"),
                                            ("/declared-huge", "download_exceeds_bound")])
def test_a_body_whose_size_cannot_be_held_to_max_bytes_is_refused_and_leaves_nothing(sites, path, status) -> None:
    base, _, handle = sites
    with pytest.raises(OpFailed) as refused:
        _download(handle, base + path, max_bytes=1024 * 1024)
    assert refused.value.status == status
    assert "max_bytes" in refused.value.message
    assert list(handle.staging_dir.iterdir()) == []


def test_a_body_longer_than_its_declared_length_is_read_only_to_that_length(sites) -> None:
    base, _, handle = sites
    receipt = _download(handle, base + "/lying", max_bytes=1024 * 1024)
    assert receipt["bytes"] == 1000


def test_the_engine_is_launched_with_downloads_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOOL_BROWSER_SCRATCH_ROOT", str(tmp_path))
    seen: dict = {}

    class _Stop(Exception):
        pass

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

    handle = sessions.SessionHandle(session="nodl", engine_binary="engine.exe")
    handle._thread.join(timeout=10)
    assert seen, "the engine was never launched"
    assert seen.get("accept_downloads") is False
