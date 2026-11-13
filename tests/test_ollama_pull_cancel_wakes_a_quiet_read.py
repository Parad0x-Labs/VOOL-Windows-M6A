"""Cancel stops a model pull at once, even while Ollama is silent (follow-up to R1-1).

R1-1 gave the quiet verify/manifest phase a 20-minute allowance between lines. Cancel was only looked
at when a line arrived, so a Cancel pressed during that silence (or during a stalled download) waited
for Ollama's next line. Cancel must wake the blocked read from the cancelling thread, hang up on
Ollama, and report "cancelled", never a failure.

The fake Ollama speaks HTTP/1.1 with chunked transfer encoding, as the real one does, so a hang-up
mid-stream looks to the reader as it would against Ollama.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import local_model_pull, local_ollama_inventory, policy_engine
from core.web.api import runtime as web_runtime

_REAL_INVENTORY_PAYLOAD = local_ollama_inventory._ollama_models_payload
OFFERED = ("qwen3:8b", "qwen2.5:7b")


class _ChunkedOllama:
    """/api/pull: manifest, byte progress, "verifying sha256 digest", then ``quiet_after`` silence."""

    def __init__(self, *, quiet_after: str, quiet_seconds: float) -> None:
        self.quiet_after, self.quiet_seconds = quiet_after, quiet_seconds
        self.installed: list[str] = []
        self.client_hung_up = threading.Event()
        self.lines_sent: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                return

            def do_GET(self):
                body = json.dumps({"models": [{"name": n, "model": n, "size": 1} for n in owner.installed]}).encode()
                self.send_response(200 if self.path == "/api/tags" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _gone(self) -> bool:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                try:
                    return self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _chunk(self, event: dict) -> None:
                data = (json.dumps(event) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                owner.lines_sent.append(str(event.get("status")))

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                model = json.loads(self.rfile.read(length) or b"{}").get("model", "")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                total = 4_870_000_000
                events = [{"status": "pulling manifest"}]
                events += [{"status": "pulling a3de86cd1c13", "total": total, "completed": total * s // 4}
                           for s in range(1, 5)]
                events += [{"status": "verifying sha256 digest"}, {"status": "writing manifest"},
                           {"status": "success"}]
                try:
                    for event in events:
                        self._chunk(event)
                        if event["status"] == owner.quiet_after:
                            deadline = time.monotonic() + owner.quiet_seconds
                            while time.monotonic() < deadline:
                                if self._gone():
                                    owner.client_hung_up.set()
                                    return
                                time.sleep(0.02)
                        if event["status"] == "writing manifest":
                            owner.installed.append(model)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    owner.client_hung_up.set()

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama(monkeypatch):
    fakes: list[_ChunkedOllama] = []
    registered: list[str] = []
    audit: list[tuple[str, dict]] = []

    def make(**kwargs) -> _ChunkedOllama:
        fake = _ChunkedOllama(**kwargs)
        fakes.append(fake)
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        return fake

    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload", _REAL_INVENTORY_PAYLOAD)
    # Both idle bounds far longer than the test: only Cancel can end the silence in time.
    monkeypatch.setattr(web_runtime, "_OLLAMA_PULL_READ_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(web_runtime, "_OLLAMA_PULL_VERIFY_IDLE_SECONDS", 60.0)
    monkeypatch.setattr(local_model_pull, "_plan", lambda: (OFFERED, 500.0))
    monkeypatch.setattr(local_model_pull, "_register", lambda model: registered.append(model))
    monkeypatch.setattr(
        "core.audit_logger.log",
        lambda event_type, target_id=None, details=None, **kw: audit.append((event_type, dict(details or {}))),
    )
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    monkeypatch.delenv("VOOL_INSTALLED_OLLAMA_MODELS", raising=False)
    local_model_pull._reset_for_tests()
    yield type("Rig", (), {"make": staticmethod(make), "registered": registered, "audit": audit})
    local_model_pull.cancel()
    local_model_pull._wait_for_tests(15.0)
    local_model_pull._reset_for_tests()
    for fake in fakes:
        fake.close()


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the fake never reached the quiet phase"
        time.sleep(0.01)


def _cancel_in_silence(ollama, *, quiet_after: str) -> tuple[dict, float, _ChunkedOllama]:
    fake = ollama.make(quiet_after=quiet_after, quiet_seconds=8.0)
    assert local_model_pull.start("qwen3:8b")[0] == 202
    _wait_until(lambda: (local_model_pull.status() or {}).get("detail") == quiet_after)
    time.sleep(0.2)  # the reader is now blocked in recv
    started = time.monotonic()
    status, payload = local_model_pull.cancel()
    assert status == 200 and payload["ok"] is True
    assert local_model_pull._wait_for_tests(6.0), "the pull never finished"
    return dict(local_model_pull.status() or {}), time.monotonic() - started, fake


def test_cancel_during_a_quiet_verify_stops_at_once_and_says_cancelled(ollama) -> None:
    final, waited, fake = _cancel_in_silence(ollama, quiet_after="verifying sha256 digest")
    assert waited < 1.5, f"Cancel waited {waited:.1f}s for Ollama's next line"
    assert final["status"] == "cancelled", final
    assert final["detail"] == "download cancelled"
    assert fake.client_hung_up.wait(3), "VOOL did not hang up on Ollama"
    assert "writing manifest" not in fake.lines_sent
    assert ollama.registered == []
    assert [d for e, d in ollama.audit if e == "model_pull_user_requested"][-1]["outcome"] == "cancelled"


def test_cancel_during_a_stalled_download_stops_at_once_and_says_cancelled(ollama) -> None:
    final, waited, fake = _cancel_in_silence(ollama, quiet_after="pulling manifest")
    assert waited < 1.5, f"Cancel waited {waited:.1f}s for Ollama's next line"
    assert final["status"] == "cancelled", final
    assert fake.client_hung_up.wait(3)
    assert [d for e, d in ollama.audit if e == "model_pull_user_requested"][-1]["outcome"] == "cancelled"


def test_after_a_cancel_the_slot_is_free_for_the_next_pull(ollama) -> None:
    _cancel_in_silence(ollama, quiet_after="verifying sha256 digest")
    assert local_model_pull.start("qwen2.5:7b")[0] == 202
