"""The installed-slice harness only ever talks to the install on this machine.

tools/installed_slices.py checked --base-url in main() only, then used urllib.request.urlopen, which
follows a redirect to any host and sends through whatever proxy the environment names. So a stand-in
on the slice port, or a proxy setting on the runner, could make the harness reach past loopback. Every
request now refuses a non-loopback URL before anything is opened, never follows a redirect, and never
uses a proxy. These tests serve stand-ins on loopback only.
"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools import installed_slices

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class _Server:
    """A loopback server that records each request line and answers with one fixed response."""

    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b'{"ok": true}') -> None:
        self.seen: list[str] = []
        seen, headers_ = self.seen, headers or {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.requestline)
                self.send_response(status)
                for key, value in headers_.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def no_sockets(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.mark.parametrize(
    "url",
    ["http://example.invalid/healthz", "https://127.0.0.1:1/healthz", "http://10.0.0.1:11435/healthz", "file:///etc/passwd"],
)
def test_a_url_that_is_not_http_loopback_is_refused_before_anything_is_opened(no_sockets, url):
    with pytest.raises(ValueError):
        installed_slices._get_json(url)


def test_run_against_a_remote_base_fails_every_slice_without_opening_a_connection(no_sockets):
    report = installed_slices.run("http://example.invalid", COMMIT)

    assert report["status"] == "fail"
    assert all(row["evidence"]["http_status"] == 0 for row in report["slices"])


def test_a_redirect_is_never_followed():
    elsewhere = _Server()
    bouncer = _Server(status=302, headers={"Location": elsewhere.url + "/healthz"})
    try:
        ok, evidence = installed_slices._check_health(bouncer.url)
    finally:
        bouncer.close()
        elsewhere.close()

    assert not ok and evidence["http_status"] == 302
    assert elsewhere.seen == []


def test_no_proxy_from_the_environment_is_used(monkeypatch):
    proxy = _Server()
    api = _Server(body=b'{"ok": true}')
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy.url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    try:
        ok, _ = installed_slices._check_health(api.url)
    finally:
        api.close()
        proxy.close()

    assert ok
    assert proxy.seen == [] and api.seen == ["GET /healthz HTTP/1.1"]
