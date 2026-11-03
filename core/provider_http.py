"""Requests-compatible provider I/O with a parent-owned transfer lifetime.

Socket inactivity timeouts are not deadlines. Only the HTTP transfer runs in a
spawned worker; admission, credentials, interpretation and publication stay in
the runtime. No request data is placed in argv, environment or temporary files.
"""
from __future__ import annotations

import base64
import errno
import ipaddress
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

import requests as _requests
from requests import HTTPError as HTTPError
from requests import exceptions as exceptions

_MAX_BODY = 64 * 1024 * 1024
_MAX_FRAME = 128 * 1024


def is_loopback_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url))
        host = str(parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"}:
            return False
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


#: Loopback (host, port) endpoints that failed to CONNECT, with the time of that failure.
#: A refused loopback connect costs about 2 s on Windows (Linux refuses at once), and with no
#: local model server listening each turn dialled 127.0.0.1:11434 several times from separate
#: subsystems (resident models, inventory, certification identity): the gauntlet's
#: harness_control group took 674.5 s on Windows against its 600 s budget (Steve, cc81b925).
#: Inside the window a bodiless GET probe of that endpoint (/api/ps, /api/tags, /api/version
#: and the like) fails exactly as a refused connect does, without dialling. Every other request
#: -- a chat POST, a model pull -- still dials, so a model server that has just started is used
#: at once; any connect that succeeds clears the memory for the probes too. Known effect: for up
#: to 10 s after a refused probe, a local server that has just started can still read as down to
#: probe-driven choices (Auto may pick its cloud fallback in that window, under all its gates). "Failed to connect" is a refusal or a connect-phase timeout: Windows retries the SYN
#: after an RST, so a refusal reaches the caller only after ~2.03 s and a 2 s connect timeout
#: fires first (Steve, d3b25bab). A live loopback server accepts at once, so on loopback a
#: connect timeout means nothing is accepting. Read timeouts, HTTP errors and non-loopback URLs
#: are never remembered, and the window is short so a local server that starts is reached again
#: within seconds.
_REFUSED_LOOPBACK_TTL_S = 10.0
_REFUSED_LOOPBACK: dict[tuple[str, int], float] = {}
_REFUSED_LOOPBACK_LOCK = threading.Lock()
_refused_clock = time.monotonic


def _loopback_endpoint(url):
    if not is_loopback_url(url):
        return None
    try:
        parsed = urllib.parse.urlparse(str(url))
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return None
    return (str(parsed.hostname or "").lower(), int(port))


def _recently_refused(endpoint):
    if endpoint is None:
        return False
    with _REFUSED_LOOPBACK_LOCK:
        refused_at = _REFUSED_LOOPBACK.get(endpoint)
        if refused_at is None:
            return False
        if _refused_clock() - refused_at < _REFUSED_LOOPBACK_TTL_S:
            return True
        _REFUSED_LOOPBACK.pop(endpoint, None)
        return False


def _note_refused(endpoint):
    if endpoint is not None:
        with _REFUSED_LOOPBACK_LOCK:
            _REFUSED_LOOPBACK[endpoint] = _refused_clock()


def _forget_refused(endpoint):
    """A connect that succeeded proves the endpoint is up again: probes may dial it at once."""
    if endpoint is not None:
        with _REFUSED_LOOPBACK_LOCK:
            _REFUSED_LOOPBACK.pop(endpoint, None)


def _connect_failed(exc):
    """True when ``exc`` is a connect-phase failure: a requests ConnectTimeout, or (anywhere in
    what it wraps) a refused TCP connect."""
    if isinstance(exc, exceptions.ConnectTimeout):
        return True
    return _refused_connect(exc)


def _refused_connect(exc):
    """True when ``exc`` (or anything it wraps) is a refused TCP connect."""
    seen = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        # ``reason`` is a plain string on HTTPError and some URLErrors; only exceptions nest.
        if not isinstance(current, BaseException) or id(current) in seen or len(seen) > 16:
            continue
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        # Explicit wrapping only (``reason``, ``raise ... from``, args): an implicit __context__
        # can be an unrelated refusal that happened to be in flight when this error was raised.
        pending.extend([getattr(current, "reason", None), current.__cause__])
        pending.extend(arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException))
    return False


def _refusal_message(endpoint):
    host, port = endpoint
    return f"loopback endpoint {host}:{port} could not be connected to within the last {_REFUSED_LOOPBACK_TTL_S:.0f} s"


class _RefuseLoopbackRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "loopback provider redirect refused", headers, fp)


def urlopen(request, *, timeout):
    """Loopback means a direct local socket, with no ambient proxy or redirects."""
    url = getattr(request, "full_url", request)
    if is_loopback_url(url):
        endpoint = _loopback_endpoint(url)
        probe = isinstance(request, str) or (request.get_method() == "GET" and request.data is None)
        if probe and _recently_refused(endpoint):
            raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, _refusal_message(endpoint)))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseLoopbackRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError:
            _forget_refused(endpoint)  # an HTTP status means the connect succeeded
            raise
        except urllib.error.URLError as exc:
            # urllib wraps only connect/send-phase errors in URLError, so a TimeoutError here is
            # a connect timeout, never a slow response.
            # A timeout counts only for a bodiless probe, whose send is trivial; a large POST that
            # times out while sending says nothing about whether the endpoint is up.
            if (probe and isinstance(exc.reason, TimeoutError)) or _refused_connect(exc):
                _note_refused(endpoint)
            raise
        _forget_refused(endpoint)
        return response
    return urllib.request.urlopen(request, timeout=timeout)


class _Transfer:
    def __init__(self, method, url, options):
        timeout = options.get("timeout", 30.0)
        budget = timeout[-1] if isinstance(timeout, (tuple, list)) else timeout
        self.deadline = time.monotonic() + max(0.001, float(budget))
        self.stopped = threading.Event()
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.frames = queue.Queue(maxsize=8)
        wire = json.dumps({"method": method, "url": url, "options": options,
                           "deadline": self.deadline}).encode()
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT"):
            env.pop(name, None)
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-B", "-s", "-P", str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.writer = threading.Thread(target=self._write, args=(wire,), name="provider-http-input")
        self.reader = threading.Thread(target=self._read, name="provider-http-output")
        self.watchdog = threading.Thread(target=self._watch, name="provider-http-deadline")
        try:
            self.writer.start()
            self.reader.start()
            self.watchdog.start()
        except BaseException:
            self.close()
            raise

    def _put(self, frame):
        while not self.stopped.is_set():
            try:
                self.frames.put(frame, timeout=0.05)
                return
            except queue.Full:
                continue

    def _write(self, wire):
        try:
            self.process.stdin.write(wire)
            self.process.stdin.close()
        except (OSError, ValueError):
            self._put({"type": "error", "class": "ConnectionError"})

    def _read(self):
        try:
            while not self.stopped.is_set():
                line = self.process.stdout.readline(_MAX_FRAME + 1)
                if not line:
                    self._put({"type": "eof"})
                    return
                if len(line) > _MAX_FRAME:
                    raise ValueError("oversized worker frame")
                self._put(json.loads(line))
        except (OSError, ValueError):
            self._put({"type": "error", "class": "ConnectionError"})

    def _terminate(self):
        with self.lock:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2.0)

    def _watch(self):
        if not self.stopped.wait(max(0, self.deadline - time.monotonic())):
            self.expired.set()
            self.stopped.set()
            self._terminate()

    def receive(self):
        while True:
            remaining = self.deadline - time.monotonic()
            if self.expired.is_set() or remaining <= 0:
                raise exceptions.Timeout("provider transfer exceeded its wall-clock deadline")
            try:
                frame = self.frames.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if frame.get("type") == "error":
                error = frame.get("class")
                if frame.get("refused") is True:
                    _note_refused(getattr(self, "endpoint", None))
                cls = (
                    exceptions.Timeout if error in {"Timeout", "ReadTimeout", "ConnectTimeout"}
                    else exceptions.TooManyRedirects if error == "TooManyRedirects"
                    else exceptions.ConnectionError
                )
                raise cls("provider HTTP worker failed: " + str(error))
            if frame.get("type") == "eof":
                if time.monotonic() >= self.deadline or self.process.poll() == 124:
                    raise exceptions.Timeout("provider transfer exceeded its wall-clock deadline")
                raise exceptions.ConnectionError("provider HTTP worker exited before completing its response")
            return frame

    def close(self):
        self.stopped.set()
        self._terminate()
        for thread in (self.writer, self.reader, self.watchdog):
            if thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
                if thread.is_alive():
                    raise RuntimeError("provider HTTP worker cleanup did not finish")
        for pipe in (self.process.stdin, self.process.stdout):
            with suppress(OSError):
                pipe.close()


class _Response(_requests.Response):
    def __init__(self, owner, header):
        super().__init__()
        self.owner = owner
        self.status_code = int(header["status"])
        self.headers.update(header["headers"])
        self.url = header["url"]
        self.encoding = _requests.utils.get_encoding_from_headers(self.headers)

    @property
    def content(self):
        if self._content is False:
            if self._content_consumed:
                raise exceptions.StreamConsumedError()
            self._content = b"".join(self.iter_content())
        return self._content

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if self._content_consumed:
            if isinstance(self._content, bool):
                raise exceptions.StreamConsumedError()
            iterator = _requests.utils.iter_slices(self._content, chunk_size)
            return _requests.utils.stream_decode_response_unicode(iterator, self) if decode_unicode else iterator

        def chunks():
            total = 0
            try:
                while True:
                    frame = self.owner.receive()
                    if frame["type"] == "done":
                        self._content_consumed = True
                        return
                    if frame["type"] != "chunk":
                        raise exceptions.ConnectionError("unexpected provider worker frame")
                    chunk = base64.b64decode(frame["data"], validate=True)
                    total += len(chunk)
                    if total > _MAX_BODY:
                        raise exceptions.ConnectionError("provider response exceeded the body limit")
                    yield chunk
            finally:
                self.close()
        iterator = chunks()
        return _requests.utils.stream_decode_response_unicode(iterator, self) if decode_unicode else iterator

    def close(self):
        self.owner.close()


def _request(method, url, **options):
    streaming = bool(options.pop("stream", False))
    endpoint = _loopback_endpoint(url)
    probe = str(method).upper() == "GET" and not any(options.get(k) for k in ("data", "json", "files"))
    if probe and _recently_refused(endpoint):
        raise exceptions.ConnectionError("provider HTTP worker failed: " + _refusal_message(endpoint))
    owner = _Transfer(method, url, options)
    owner.endpoint = endpoint
    try:
        header = owner.receive()
        if header.get("type") != "response":
            raise exceptions.ConnectionError("provider worker did not return response headers")
        _forget_refused(endpoint)
        response = _Response(owner, header)
        if not streaming:
            _ = response.content
        return response
    except BaseException:
        owner.close()
        raise


def post(url, **options):
    return _request("POST", url, **options)


def get(url, **options):
    return _request("GET", url, **options)


#: The only caller headers that may follow a redirect to ANOTHER origin. ``requests`` strips
#: ``Authorization`` there by itself, but a key placed in any other header (a custom endpoint's
#: ``api-key`` / ``x-api-key``) would be re-sent to whatever host the redirect names.
_CROSS_ORIGIN_SAFE_HEADERS = frozenset({"accept", "accept-encoding", "content-type", "user-agent"})


class _KeySafeSession(_requests.Session):
    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            for name in [n for n in prepared_request.headers if n.lower() not in _CROSS_ORIGIN_SAFE_HEADERS]:
                del prepared_request.headers[name]


def _worker():
    def emit(frame):
        print(json.dumps(frame, separators=(",", ":")), flush=True)

    timer = None
    try:
        invocation = json.load(sys.stdin)
        # If the runtime itself exits, the transfer still cannot outlive its grant.
        timer = threading.Timer(max(0, invocation["deadline"] - time.monotonic()), lambda: os._exit(124))
        timer.daemon = True
        timer.start()
        options = invocation["options"]
        if isinstance(options.get("timeout"), list):
            options["timeout"] = tuple(options["timeout"])
        loopback = is_loopback_url(invocation["url"])
        return_redirect = options.get("allow_redirects") is False
        # Each transfer owns its session. A local invocation never changes the
        # proxy/redirect behavior of a later explicitly authorized cloud call.
        with _KeySafeSession() as session:
            if loopback:
                session.trust_env = False
                options.pop("proxies", None)
                options["allow_redirects"] = False
            with session.request(invocation["method"], invocation["url"], stream=True, **options) as response:
                if loopback and response.is_redirect and not return_redirect:
                    raise exceptions.TooManyRedirects("loopback provider redirect refused")
                emit({"type": "response", "status": response.status_code,
                      "headers": dict(response.headers), "url": response.url})
                for chunk in response.iter_content(chunk_size=512):
                    emit({"type": "chunk", "data": base64.b64encode(chunk).decode("ascii")})
                emit({"type": "done"})
    except Exception as exc:
        frame = {"type": "error", "class": type(exc).__name__}
        if _connect_failed(exc):
            frame["refused"] = True
        emit(frame)
    finally:
        if timer is not None:
            timer.cancel()


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
