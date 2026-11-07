"""A user's click downloads one model the plan offers, and nothing else (clean-VM job 52, design D2).

Job 52 (port 76afbd36): a new Windows user could not get a model from VOOL at all. The only pull runs
at boot and is skipped while ``network.outbound_enabled`` keeps its default of false, and Settings told
the user to run ``ollama pull`` in a terminal. Jesse (7 Nov 05:14Z): "click-to-download counts as
consent". 🔐's binding conditions, each pinned here:

* the model must exactly match a name:tag the machine-sized plan offers; no free text, host or registry;
* the local-session guard of the other state doors (JSON, same-origin, loopback peer) applies;
* local-only (offline) mode and a local-models-disabled policy win over the click; the click overrides
  only the default outbound_enabled=false, and never changes that setting;
* one pull at a time, Cancel really stops the download, and the disk check runs before it starts;
* the audit record carries the model, its size and the outcome.

The pull talks to a fake Ollama on loopback, so the cancel test proves the HTTP stream is closed.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import local_model_pull, policy_engine
from core.web.api.runtime import RuntimeServices
from core.web.api.service import dispatch_get, dispatch_post

OFFERED = ("qwen3:8b", "qwen2.5:7b")


class _FakeOllama:
    """/api/tags lists `installed`; /api/pull streams `steps` progress lines, `delay` seconds apart."""

    def __init__(self, *, steps: int = 5, delay: float = 0.0, installed: tuple[str, ...] = ()) -> None:
        self.steps, self.delay, self.installed = steps, delay, list(installed)
        self.pulls: list[dict] = []
        self.disconnected = threading.Event()
        self.lines_sent = 0
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

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.pulls.append(json.loads(self.rfile.read(length) or b"{}"))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for i in range(owner.steps):
                        line = {"status": "pulling", "total": owner.steps, "completed": i + 1}
                        self.wfile.write((json.dumps(line) + "\n").encode())
                        self.wfile.flush()
                        owner.lines_sent += 1
                        time.sleep(owner.delay)
                    self.wfile.write(b'{"status":"success"}\n')
                    self.wfile.flush()
                    owner.installed.append(owner.pulls[-1].get("model", ""))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    owner.disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def rig(monkeypatch):
    fakes: list[_FakeOllama] = []
    audit: list[tuple[str, dict]] = []
    registered: list[str] = []

    def make(**kwargs) -> _FakeOllama:
        fake = _FakeOllama(**kwargs)
        fakes.append(fake)
        monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
        # tests/conftest.py stubs the shared inventory boundary to "nothing installed" for every
        # test, so the fake's own installed list stands in for /api/tags here.
        monkeypatch.setattr(
            local_model_pull, "_installed",
            lambda base_url: {n if ":" in n else f"{n}:latest" for n in fake.installed} if base_url == fake.url else set(),
        )
        return fake

    monkeypatch.setattr(local_model_pull, "_plan", lambda: (OFFERED, 500.0))
    monkeypatch.setattr(
        "core.audit_logger.log",
        lambda event_type, target_id=None, details=None, **kw: audit.append((event_type, dict(details or {}))),
    )
    monkeypatch.setattr(local_model_pull, "_register", lambda model: registered.append(model))
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    monkeypatch.delenv("VOOL_INSTALLED_OLLAMA_MODELS", raising=False)
    local_model_pull._reset_for_tests()
    yield type("Rig", (), {"make": staticmethod(make), "audit": audit, "registered": registered})
    local_model_pull.cancel()
    local_model_pull._wait_for_tests(5.0)
    local_model_pull._reset_for_tests()
    for fake in fakes:
        fake.close()


def _finished(timeout: float = 10.0) -> dict:
    assert local_model_pull._wait_for_tests(timeout), "the pull never finished"
    return dict(local_model_pull.status() or {})


def test_the_offer_is_the_plan_with_sizes_and_the_registry_named(rig) -> None:
    rig.make(installed=("qwen2.5:7b",))
    offer = local_model_pull.offered()
    assert [row["model"] for row in offer["models"]] == list(OFFERED)
    assert all(row["size_gb"] > 0 for row in offer["models"])
    assert [row["installed"] for row in offer["models"]] == [False, True]
    assert "Ollama" in offer["source"]


@pytest.mark.parametrize(
    "model",
    ["qwen3:14b", "qwen3", "QWEN3:8B", " qwen3:8b", "registry.ollama.ai/library/qwen3:8b",
     "http://evil.example/qwen3:8b", "", None, 8, ["qwen3:8b"]],
)
def test_only_an_exact_offered_name_and_tag_is_pulled(rig, model) -> None:
    fake = rig.make()
    status, payload = local_model_pull.start(model)
    assert status == 400 and payload["error"] == "model_not_offered"
    assert fake.pulls == []


def test_local_only_mode_wins_over_the_click(rig, monkeypatch) -> None:
    fake = rig.make()
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: True)
    status, payload = local_model_pull.start("qwen3:8b")
    assert status == 409 and payload["error"] == "offline_mode"
    assert fake.pulls == []


def test_a_disabled_local_model_policy_wins_over_the_click(rig, monkeypatch) -> None:
    fake = rig.make()
    monkeypatch.setattr(local_model_pull, "_local_models_disabled", lambda: True)
    status, payload = local_model_pull.start("qwen3:8b")
    assert status == 409 and payload["error"] == "local_models_disabled"
    assert fake.pulls == []


def test_ollama_must_be_on_this_computer(rig, monkeypatch) -> None:
    rig.make()
    monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", "http://10.0.0.9:11434")
    status, payload = local_model_pull.start("qwen3:8b")
    assert status == 409 and payload["error"] == "ollama_not_local"


def test_the_disk_check_runs_before_any_download(rig, monkeypatch) -> None:
    fake = rig.make()
    monkeypatch.setattr(local_model_pull, "_plan", lambda: (OFFERED, 3.0))
    status, payload = local_model_pull.start("qwen3:8b")
    assert status == 507 and payload["error"] == "insufficient_space"
    assert payload["needed_gb"] > payload["free_gb"] == 3.0
    assert fake.pulls == []


def test_the_click_downloads_without_turning_outbound_on(rig) -> None:
    fake = rig.make(steps=3)
    before = policy_engine.get("network.outbound_enabled", False)
    runtime = RuntimeServices()
    status, payload = local_model_pull.start("qwen3:8b", runtime=runtime)
    assert status == 202 and payload["ok"] is True
    final = _finished()
    assert final["status"] == "ready" and final["percent"] == 100
    assert fake.pulls == [{"model": "qwen3:8b", "stream": True}]
    assert rig.registered == ["qwen3:8b"]
    # Setup and Settings read progress where they already read the boot pull.
    assert runtime.runtime_version_stamp["model_pull"]["model"] == "qwen3:8b"
    assert policy_engine.get("network.outbound_enabled", False) == before
    outcomes = [d for e, d in rig.audit if e == "model_pull_user_requested"]
    assert outcomes and outcomes[-1]["model"] == "qwen3:8b"
    assert outcomes[-1]["size_gb"] > 0 and outcomes[-1]["outcome"] == "succeeded"


def test_an_installed_model_is_not_downloaded_again(rig) -> None:
    fake = rig.make(installed=("qwen3:8b",))
    status, payload = local_model_pull.start("qwen3:8b")
    assert status == 200 and payload["status"] == "ready"
    assert fake.pulls == []


def test_one_pull_at_a_time_and_cancel_closes_the_download(rig) -> None:
    fake = rig.make(steps=400, delay=0.02)
    assert local_model_pull.start("qwen3:8b")[0] == 202
    deadline = time.monotonic() + 5
    while fake.lines_sent < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    status, payload = local_model_pull.start("qwen2.5:7b")
    assert status == 409 and payload["error"] == "pull_in_progress"

    status, payload = local_model_pull.cancel()
    assert status == 200 and payload["ok"] is True
    final = _finished()
    assert final["status"] == "cancelled"
    # The stream was closed from our side, so Ollama stops downloading.
    assert fake.disconnected.wait(5), "VOOL kept reading the pull after Cancel"
    assert fake.lines_sent < 400
    outcomes = [d for e, d in rig.audit if e == "model_pull_user_requested"]
    assert outcomes[-1]["outcome"] == "cancelled"
    assert rig.registered == []
    # The slot is free again.
    assert local_model_pull.start("qwen2.5:7b")[0] == 202


def test_cancel_with_nothing_running_says_so(rig) -> None:
    rig.make()
    status, payload = local_model_pull.cancel()
    assert status == 409 and payload["error"] == "no_pull_running"


def test_an_ollama_error_line_fails_the_pull_and_is_audited(rig, monkeypatch) -> None:
    fake = rig.make()

    def refuse(self):  # an Ollama that answers the pull with an error line
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"error":"pull model manifest: file does not exist"}\n')

    monkeypatch.setattr(fake.server.RequestHandlerClass, "do_POST", refuse)
    assert local_model_pull.start("qwen3:8b")[0] == 202
    final = _finished()
    assert final["status"] == "failed" and "manifest" in final["detail"]
    assert [d for e, d in rig.audit if e == "model_pull_user_requested"][-1]["outcome"] == "failed"


# --- the HTTP door ------------------------------------------------------------------------------

def _post(path: str, body, *, client_host: str = "127.0.0.1", headers=None):
    return dispatch_post(
        path=path,
        body=body,
        headers=headers if headers is not None else {"Content-Type": "application/json"},
        client_host=client_host,
        runtime=RuntimeServices(),
        model_name="vool",
        workspace_root_provider=lambda: "/tmp",
    )


def _json(response) -> dict:
    return json.loads(bytes(response.body or b"{}").decode("utf-8"))


def test_the_door_keeps_the_local_session_guard(rig) -> None:
    fake = rig.make()
    assert _post("/api/models/local/pull", {"model": "qwen3:8b"}, client_host="10.0.0.4").status == 403
    cross = _post("/api/models/local/pull", {"model": "qwen3:8b"},
                  headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert cross.status == 403
    form = _post("/api/models/local/pull", {"model": "qwen3:8b"},
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status == 415
    extra = _post("/api/models/local/pull", {"model": "qwen3:8b", "registry": "https://evil.example"})
    assert extra.status == 400
    assert fake.pulls == []


def test_the_door_starts_reports_and_cancels(rig) -> None:
    rig.make(steps=400, delay=0.02)
    started = _post("/api/models/local/pull", {"model": "qwen3:8b"})
    assert started.status == 202 and _json(started)["ok"] is True
    shown = dispatch_get(
        path="/api/models/local/pull", query={}, headers={}, runtime=RuntimeServices(), model_name="vool",
        client_host="127.0.0.1",
    )
    view = _json(shown)
    assert shown.status == 200 and view["pull"]["model"] == "qwen3:8b"
    assert [row["model"] for row in view["models"]] == list(OFFERED)
    cancelled = _post("/api/models/local/pull/cancel", {})
    assert cancelled.status == 200
    assert _finished()["status"] == "cancelled"
