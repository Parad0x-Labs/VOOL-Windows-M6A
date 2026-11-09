"""A model pull is not cut off while Ollama verifies and registers what it downloaded (R1-1, ship bar M6).

Capability test R1-1 (fresh Windows 11 VM, build 828b8bda): the first "Download qwen3:8b" in Setup
stopped after about six minutes with "Download stopped: timed out". All 4.87 GB had arrived. Ollama
then sent one "verifying sha256 digest" line and went quiet while it hashed the blob on a slow disk,
and VOOL's 120 s per-read timeout hung up before "writing manifest", so the model was never
registered. A second click finished in 17 s.

The fake Ollama here streams the real phase order (pulling manifest, byte progress, verifying sha256
digest, writing manifest, success) and can stay silent inside any phase. The download-phase stall
bound is shrunk so a "long" verify takes about a second, not minutes.
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
STALL_BOUND = 0.3


class _StreamingOllama:
    """/api/pull streams Ollama's phases; ``silence`` maps a phase to seconds of quiet after its line
    ("downloading N" is the N-th of five byte-progress lines).

    ``register_after`` is the phase after which the model shows in /api/tags (Ollama wrote the
    manifest), with ``register_delay`` seconds into that phase's silence. A client that hung up
    before then leaves the model unregistered, as a cancelled Ollama pull does.
    """

    PHASES = ("pulling manifest", "downloading", "verifying sha256 digest", "writing manifest", "success")

    def __init__(self, *, silence: dict[str, float] | None = None, register_after: str = "writing manifest",
                 register_delay: float = 0.0, send_success: bool = True) -> None:
        self.silence = dict(silence or {})
        self.register_after, self.register_delay, self.send_success = register_after, register_delay, send_success
        self.installed: list[str] = []
        self.pulls: list[dict] = []
        self.client_hung_up = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                return

            def do_GET(self):
                if self.path != "/api/tags":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"models": [{"name": n, "model": n, "size": 1} for n in owner.installed]}).encode()
                self.send_response(200)
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

            def _quiet(self, seconds: float, *, register_at: float | None) -> bool:
                """Stay silent; register the model ``register_at`` seconds in. False if the client left."""
                start = time.monotonic()
                registered = register_at is None
                while True:
                    elapsed = time.monotonic() - start
                    if not registered and elapsed >= register_at:
                        if self._gone():
                            owner.client_hung_up.set()
                            return False
                        owner.installed.append(owner.pulls[-1].get("model", ""))
                        registered = True
                    if elapsed >= seconds:
                        break
                    time.sleep(0.02)
                if self._gone():
                    owner.client_hung_up.set()
                    return False
                return True

            def _line(self, event: dict) -> None:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.pulls.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                total = 4_870_000_000
                try:
                    for phase in owner.PHASES:
                        if phase == "success" and not owner.send_success:
                            return
                        if phase == "downloading":
                            for step in range(1, 6):
                                self._line({"status": "pulling a3de86cd1c13", "digest": "sha256:a3de86cd1c13",
                                            "total": total, "completed": total * step // 5})
                                if not self._quiet(owner.silence.get(f"downloading {step}", 0.0), register_at=None):
                                    return
                        else:
                            self._line({"status": phase})
                        register_at = owner.register_delay if phase == owner.register_after else None
                        if not self._quiet(owner.silence.get(phase, 0.0), register_at=register_at):
                            return
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
    fakes: list[_StreamingOllama] = []
    registered: list[str] = []
    audit: list[tuple[str, dict]] = []

    def make(**kwargs) -> _StreamingOllama:
        fake = _StreamingOllama(**kwargs)
        fakes.append(fake)
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        return fake

    # The real /api/tags read, against the fake (tests/conftest.py stubs it to "nothing installed").
    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload", _REAL_INVENTORY_PAYLOAD)
    monkeypatch.setattr(web_runtime, "_OLLAMA_PULL_READ_TIMEOUT_SECONDS", STALL_BOUND)
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
    local_model_pull._wait_for_tests(10.0)
    local_model_pull._reset_for_tests()
    for fake in fakes:
        fake.close()


def _verify_allowance(monkeypatch, seconds: float) -> None:
    # The fix's allowance for the quiet verify/register phase; absent before the fix.
    monkeypatch.setattr(web_runtime, "_OLLAMA_PULL_VERIFY_IDLE_SECONDS", seconds, raising=False)


def _click(model: str = "qwen3:8b", timeout: float = 15.0) -> dict:
    status, payload = local_model_pull.start(model)
    assert status == 202, payload
    assert local_model_pull._wait_for_tests(timeout), "the pull never finished"
    return dict(local_model_pull.status() or {})


def test_a_long_quiet_sha256_verify_is_not_timed_out(ollama, monkeypatch) -> None:
    # R1-1 itself: every byte arrived, then Ollama is silent far longer than the stall bound.
    _verify_allowance(monkeypatch, 10.0)
    fake = ollama.make(silence={"verifying sha256 digest": STALL_BOUND * 5})
    final = _click()
    assert final["status"] == "ready", final
    assert final["percent"] == 100
    assert not fake.client_hung_up.is_set(), "VOOL hung up on Ollama while it was verifying"
    assert fake.installed == ["qwen3:8b"]
    assert ollama.registered == ["qwen3:8b"]
    assert [d for e, d in ollama.audit if e == "model_pull_user_requested"][-1]["outcome"] == "succeeded"


def test_a_slow_manifest_write_is_not_timed_out(ollama, monkeypatch) -> None:
    _verify_allowance(monkeypatch, 10.0)
    fake = ollama.make(silence={"verifying sha256 digest": STALL_BOUND * 3, "writing manifest": STALL_BOUND * 3})
    final = _click()
    assert final["status"] == "ready", final
    assert not fake.client_hung_up.is_set()


def test_the_boot_pull_also_survives_a_long_verify(ollama, monkeypatch) -> None:
    _verify_allowance(monkeypatch, 10.0)
    fake = ollama.make(silence={"verifying sha256 digest": STALL_BOUND * 5})
    progress = web_runtime.ModelPullProgress("qwen3:8b", env={"VOOL_RAW_OLLAMA_API_URL": fake.url})
    web_runtime._stream_ollama_pull("qwen3:8b", progress=progress)
    assert fake.installed == ["qwen3:8b"]
    assert not fake.client_hung_up.is_set()


def test_a_timeout_after_the_bytes_arrived_reports_success_when_ollama_registered_it(ollama, monkeypatch) -> None:
    # Ollama wrote the manifest, but the end of the stream never came within the allowance.
    _verify_allowance(monkeypatch, 0.8)
    ollama.make(silence={"writing manifest": 4.0}, register_after="writing manifest", register_delay=0.1,
                send_success=False)
    final = _click()
    assert final["status"] == "ready", final
    assert ollama.registered == ["qwen3:8b"]


def test_a_timeout_after_the_bytes_arrived_fails_plainly_when_the_model_is_not_registered(ollama, monkeypatch) -> None:
    _verify_allowance(monkeypatch, 0.8)
    ollama.make(silence={"verifying sha256 digest": 4.0})
    final = _click()
    assert final["status"] == "failed", final
    detail = final["detail"]
    assert detail != "timed out"
    assert "timed out" not in detail.lower()
    assert "Download again" in detail
    assert ollama.registered == []
    assert [d for e, d in ollama.audit if e == "model_pull_user_requested"][-1]["outcome"] == "failed"


def test_a_stalled_download_still_stops_and_says_so_plainly(ollama, monkeypatch) -> None:
    # Before all bytes arrive, a silent Ollama is a stalled download: the short bound still applies.
    _verify_allowance(monkeypatch, 30.0)
    fake = ollama.make(silence={"pulling manifest": 3.0})
    started = time.monotonic()
    final = _click()
    assert time.monotonic() - started < 2.5, "a stalled download waited for the verify allowance"
    assert final["status"] == "failed", final
    assert "timed out" not in final["detail"].lower()
    assert "Download again" in final["detail"]
    assert fake.installed == []


def test_a_download_that_stalls_part_way_stops_on_the_short_bound(ollama, monkeypatch) -> None:
    # Bytes have started to arrive but are not all here: silence is still the network.
    _verify_allowance(monkeypatch, 30.0)
    fake = ollama.make(silence={"downloading 2": 3.0})
    started = time.monotonic()
    final = _click()
    assert time.monotonic() - started < 2.5, "a part-way stall waited for the verify allowance"
    assert final["status"] == "failed", final
    assert "no download progress" in final["detail"]
    assert "Download again" in final["detail"]
    assert fake.installed == []
