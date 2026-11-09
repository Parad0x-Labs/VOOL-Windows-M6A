"""In 0.7.1 the Ollama cloud-model refusal is Windows only, behind one flag.

``core.ollama_cloud_models.CLOUD_REFUSAL_ACTIVE`` is True on Windows. Off Windows every surface
behaves as it did before the refusal existed: a cloud name is pulled, listed, registered and sent
like any other. Whether the refusal applies on macOS and Linux is the owner's later call. The rows
with the flag on live in tests/test_ollama_cloud_models_are_never_used.py; here each surface is
checked both ways.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from core import local_ollama_inventory, policy_engine

_REAL_INVENTORY_PAYLOAD = local_ollama_inventory._ollama_models_payload

CLOUD = "gpt-oss:20b-cloud"
LOCAL = "qwen3:8b"
REMOTE = {"remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:20b"}


class _FakeOllama:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, {}))
                self._send({"models": [{"name": LOCAL, "size": 5_000_000_000},
                                       {"name": CLOUD, "size": 384, **REMOTE}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append(("POST", self.path, json.loads(self.rfile.read(length) or b"{}")))
                self._send({"status": "success"})

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama(monkeypatch, tmp_path):
    fake = _FakeOllama()
    monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "empty-store"))
    monkeypatch.delenv("VOOL_INSTALLED_OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload", _REAL_INVENTORY_PAYLOAD)
    yield fake
    fake.close()


def _flag(monkeypatch, on: bool) -> None:
    from core import ollama_cloud_models

    monkeypatch.setattr(ollama_cloud_models, "CLOUD_REFUSAL_ACTIVE", on)


def test_the_flag_is_on_exactly_on_windows() -> None:
    from core import ollama_cloud_models

    assert ollama_cloud_models.CLOUD_REFUSAL_ACTIVE is (os.name == "nt")
    assert ollama_cloud_models.active() is (os.name == "nt")


def test_the_name_reader_is_the_same_both_ways(monkeypatch) -> None:
    from core import ollama_cloud_models

    for on in (True, False):
        _flag(monkeypatch, on)
        assert ollama_cloud_models.is_cloud_model_name(CLOUD)  # what a model is never changes
        assert ollama_cloud_models.refuses_cloud_name(CLOUD) is on
        assert ollama_cloud_models.refuses_remote_entry(REMOTE) is on
        assert ollama_cloud_models.is_cloud_model(CLOUD) is on


@pytest.mark.parametrize("on", [True, False])
def test_the_shared_pull_both_ways(ollama, monkeypatch, on) -> None:
    from core.ollama_cloud_models import CloudModelRefused
    from core.web.api.runtime import ModelPullProgress, _stream_ollama_pull

    _flag(monkeypatch, on)
    if on:
        with pytest.raises(CloudModelRefused):
            _stream_ollama_pull(CLOUD, progress=ModelPullProgress(CLOUD))
        assert ollama.requests == []
    else:
        _stream_ollama_pull(CLOUD, progress=ModelPullProgress(CLOUD))
        assert [(m, p) for m, p, _b in ollama.requests] == [("POST", "/api/pull")]


@pytest.mark.parametrize("on", [True, False])
def test_the_click_both_ways(ollama, monkeypatch, on) -> None:
    from core import local_model_pull

    _flag(monkeypatch, on)
    local_model_pull._reset_for_tests()
    monkeypatch.setattr(local_model_pull, "_plan", lambda: ((LOCAL, CLOUD), 500.0))
    monkeypatch.setattr(local_model_pull, "_register", lambda model: None)
    monkeypatch.setattr(local_model_pull, "_installed", lambda base_url: set())
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    monkeypatch.setattr("core.audit_logger.log", lambda *a, **k: None)
    status, payload = local_model_pull.start(CLOUD)
    if on:
        assert (status, payload["error"]) == (400, "ollama_cloud_model_refused")
    else:
        assert status == 202
        assert local_model_pull._wait_for_tests(10)
    local_model_pull._reset_for_tests()


@pytest.mark.parametrize("on", [True, False])
def test_the_inventory_both_ways(ollama, monkeypatch, on) -> None:
    from core.local_ollama_inventory import installed_ollama_model_names

    _flag(monkeypatch, on)
    names = installed_ollama_model_names(base_url=ollama.url)
    assert names == ((LOCAL,) if on else (LOCAL, CLOUD))


@pytest.mark.parametrize("on", [True, False])
def test_the_catalog_and_chat_door_both_ways(monkeypatch, on) -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import _ollama_cloud_model_refusal, _runtime_model_catalog

    _flag(monkeypatch, on)
    listed = [row["id"] for row in _runtime_model_catalog(capability_snapshot={}, default_model_name=CLOUD)]
    assert listed == ([] if on else [CLOUD])
    refusal = _ollama_cloud_model_refusal(CLOUD, RuntimeServices())
    assert (refusal is not None) is on


@pytest.mark.parametrize("on", [True, False])
def test_the_registry_and_adapter_both_ways(monkeypatch, on) -> None:
    from types import SimpleNamespace

    from adapters.openai_compatible_adapter import OpenAICompatibleAdapter
    from core.ollama_cloud_models import CloudModelRefused, manifest_is_ollama_cloud

    _flag(monkeypatch, on)
    manifest = SimpleNamespace(provider_id=f"ollama-local:{CLOUD}", provider_name="ollama-local", model_name=CLOUD,
                               metadata={"runtime_family": "ollama"}, runtime_config={"base_url": "http://127.0.0.1:1"})
    assert manifest_is_ollama_cloud(manifest) is on
    adapter = OpenAICompatibleAdapter(manifest)
    if on:
        with pytest.raises(CloudModelRefused):
            adapter._refuse_ollama_cloud_model()
    else:
        adapter._refuse_ollama_cloud_model()


@pytest.mark.parametrize("on", [True, False])
def test_the_autopilot_both_ways(monkeypatch, on) -> None:
    from core.local_inference_autopilot import select_daily_residency_model_tag

    _flag(monkeypatch, on)
    picked = select_daily_residency_model_tag([CLOUD], default_model_tag=CLOUD)
    assert (picked == "") is on
