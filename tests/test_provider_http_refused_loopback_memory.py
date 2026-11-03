"""A loopback endpoint that refused a connection is not dialled again for a short window.

Measured on Windows (Steve, cc81b925, 3 Nov 2026): a refused loopback connect costs about 2 s
there, while Linux refuses at once. With no Ollama listening, the gauntlet's harness_control group
took 674.5 s against its 600 s budget. 44 of 77 turn intervals were over 8 s, because each turn's
main thread separately dialled 127.0.0.1:11434: resource_governor.resident_models (/api/ps),
local_ollama_inventory (/api/tags) and the certification identity read (/api/version). On Linux
the same run makes 47 such refused main-thread connects.

The invariant: once a loopback (host, port) fails to CONNECT through core.provider_http -- refused,
or (as Windows reports a refusal that outlasts a ~2 s connect timeout) a connect timeout -- the
next bodiless GET probes of that same endpoint fail without dialling, until a short window passes
or a connect to it succeeds. Real work (a POST: chat, pull) always dials, so a model server that
has just started is used at once. Other
endpoints, non-loopback URLs and failures after the connect (a read timeout, an HTTP error) are
unaffected.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import provider_http


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [5000.0]
    monkeypatch.setattr(provider_http, "_refused_clock", lambda: now[0])
    provider_http._REFUSED_LOOPBACK.clear()
    yield now
    provider_http._REFUSED_LOOPBACK.clear()


@pytest.fixture
def dials(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every loopback request urlopen actually sends (the opener it builds is the only dialler).

    Counted at the opener, not by wrapping socket functions: the suite's network seal already
    wraps those, and a second wrapper recurses through it."""
    seen: list[str] = []
    real_build = urllib.request.build_opener

    def build_opener(*handlers: object) -> urllib.request.OpenerDirector:
        opener = real_build(*handlers)
        real_open = opener.open

        def counted_open(request: object, *args: object, **kwargs: object) -> object:
            seen.append(str(getattr(request, "full_url", request)))
            return real_open(request, *args, **kwargs)

        opener.open = counted_open  # type: ignore[method-assign]
        return opener

    monkeypatch.setattr(provider_http.urllib.request, "build_opener", build_opener)
    return seen


def test_urlopen_does_not_redial_a_refused_loopback_endpoint(clock: list[float], dials: list[str]) -> None:
    url = f"http://127.0.0.1:{_closed_loopback_port()}/api/ps"
    for attempt in range(4):
        with pytest.raises(urllib.error.URLError) as info:
            provider_http.urlopen(urllib.request.Request(url), timeout=2.0)
        # The one real dial may end as a connect timeout on Windows (its refusal takes ~2.03 s);
        # every answer from the memory is the refusal itself.
        expected = (ConnectionRefusedError, TimeoutError) if attempt == 0 else ConnectionRefusedError
        assert isinstance(info.value.reason, expected), info.value.reason
    assert len(dials) == 1, dials


def test_get_does_not_start_a_transfer_for_a_refused_loopback_endpoint(
    clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    real = provider_http._Transfer

    def counting(method: str, url: str, options: dict) -> object:
        started.append(url)
        return real(method, url, options)

    monkeypatch.setattr(provider_http, "_Transfer", counting)
    url = f"http://127.0.0.1:{_closed_loopback_port()}/api/version"
    exceptions = provider_http.exceptions
    # The one real transfer may end as a connect timeout on Windows (surfaced as Timeout, as
    # before this change); every answer from the memory is a ConnectionError.
    with pytest.raises((exceptions.ConnectionError, exceptions.Timeout)):
        provider_http.get(url, timeout=(2.0, 5.0))
    for _ in range(2):
        with pytest.raises(exceptions.ConnectionError):
            provider_http.get(url, timeout=(2.0, 5.0))
    assert len(started) == 1, started


def test_the_two_entry_points_share_one_memory(
    clock: list[float], dials: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    real = provider_http._Transfer

    def counting(method: str, url: str, options: dict) -> object:
        started.append(url)
        return real(method, url, options)

    monkeypatch.setattr(provider_http, "_Transfer", counting)
    port = _closed_loopback_port()
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/ps"), timeout=2.0)
    with pytest.raises(provider_http.exceptions.ConnectionError):
        provider_http.get(f"http://127.0.0.1:{port}/api/version", timeout=(2.0, 5.0))
    assert len(dials) == 1, dials
    assert started == [], "a refusal seen by urlopen must also stop the transfer path"


def test_the_endpoint_is_dialled_again_after_the_window(clock: list[float], dials: list[str]) -> None:
    url = f"http://127.0.0.1:{_closed_loopback_port()}/api/ps"
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(urllib.request.Request(url), timeout=2.0)
    clock[0] += provider_http._REFUSED_LOOPBACK_TTL_S + 0.01
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(urllib.request.Request(url), timeout=2.0)
    assert len(dials) == 2, dials


def test_another_loopback_port_is_still_dialled(clock: list[float], dials: list[str]) -> None:
    for port in (_closed_loopback_port(), _closed_loopback_port()):
        with pytest.raises(urllib.error.URLError):
            provider_http.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/"), timeout=2.0)
    assert len(dials) == 2, dials


def test_a_listening_endpoint_is_never_remembered(clock: list[float]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        for _ in range(2):
            with pytest.raises((urllib.error.URLError, OSError, TimeoutError)):
                # Accepted but never answered: a read timeout, not a refused connect.
                provider_http.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/"), timeout=0.2)
    assert provider_http._REFUSED_LOOPBACK == {}


def test_only_loopback_urls_have_a_memory_key() -> None:
    assert provider_http._loopback_endpoint("http://127.0.0.1:11434/api/ps") == ("127.0.0.1", 11434)
    assert provider_http._loopback_endpoint("http://localhost/api/ps") == ("localhost", 80)
    assert provider_http._loopback_endpoint("https://api.example.com/v1") is None
    assert provider_http._loopback_endpoint("http://10.0.0.5:11434/") is None


def test_a_connect_timeout_counts_as_unreachable(clock: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows a refusal arrives only after ~2.03 s, so a 2 s connect timeout fires first
    (Steve, d3b25bab): urllib reports it as URLError(TimeoutError) from the connect phase."""
    attempts: list[str] = []

    class _TimingOutOpener:
        def open(self, request: object, *args: object, **kwargs: object) -> object:
            attempts.append(str(getattr(request, "full_url", request)))
            raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(provider_http.urllib.request, "build_opener", lambda *handlers: _TimingOutOpener())
    url = "http://127.0.0.1:11434/api/ps"
    for _ in range(3):
        with pytest.raises(urllib.error.URLError):
            provider_http.urlopen(urllib.request.Request(url), timeout=2.0)
    assert attempts == [url]


def test_the_worker_flags_a_connect_timeout_and_a_refusal_but_not_a_read_timeout() -> None:
    exceptions = provider_http.exceptions
    refused = exceptions.ConnectionError("max retries")
    refused.__cause__ = ConnectionRefusedError(10061, "refused")
    assert provider_http._connect_failed(refused)
    assert provider_http._connect_failed(exceptions.ConnectTimeout("connect timed out"))
    assert not provider_http._connect_failed(exceptions.ReadTimeout("read timed out"))
    assert not provider_http._connect_failed(exceptions.TooManyRedirects("redirect refused"))


def test_a_post_after_a_refusal_still_dials(clock: list[float], dials: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat turn or pull must not inherit a probe's refusal: the server may have just started."""
    started: list[str] = []
    real = provider_http._Transfer

    def counting(method: str, url: str, options: dict) -> object:
        started.append(method)
        return real(method, url, options)

    monkeypatch.setattr(provider_http, "_Transfer", counting)
    port = _closed_loopback_port()
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/ps"), timeout=2.0)
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/api/pull", data=b"{}", method="POST"), timeout=2.0
        )
    with pytest.raises((provider_http.exceptions.ConnectionError, provider_http.exceptions.Timeout)):
        provider_http.post(f"http://127.0.0.1:{port}/api/chat", json={"model": "m"}, timeout=(2.0, 5.0))
    assert len(dials) == 2, dials
    assert started == ["POST"]


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._ok()

    def do_POST(self) -> None:
        self._ok()

    def _ok(self) -> None:
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_a_successful_connect_clears_the_memory_for_probes(clock: list[float], dials: list[str]) -> None:
    port = _closed_loopback_port()
    probe = f"http://127.0.0.1:{port}/api/tags"
    with pytest.raises(urllib.error.URLError):
        provider_http.urlopen(urllib.request.Request(probe), timeout=2.0)
    server = ThreadingHTTPServer(("127.0.0.1", port), _Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        chat = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=b"{}", method="POST")
        with provider_http.urlopen(chat, timeout=2.0) as response:
            assert response.status == 200
        with provider_http.urlopen(urllib.request.Request(probe), timeout=2.0) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
    assert len(dials) == 3, dials
    assert provider_http._REFUSED_LOOPBACK == {}


def test_an_unrelated_refusal_in_flight_does_not_tag_another_error() -> None:
    try:
        try:
            raise ConnectionRefusedError(111, "some other socket")
        except ConnectionRefusedError:
            raise ValueError("malformed JSON")  # the implicit __context__ is the point
    except ValueError as exc:
        assert not provider_http._connect_failed(exc)
