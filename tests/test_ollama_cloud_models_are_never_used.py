"""VOOL never pulls, lists, certifies, routes to or runs an Ollama cloud model.

Capability test (fresh Windows 11 VM, build 828b8bda, bundled Ollama 0.35.1): the bundled
``ollama serve`` runs with ``OLLAMA_NO_CLOUD=1``, which blocks ``ollama run`` of a cloud model but
not ``ollama pull gpt-oss:20b-cloud``. The pull fetched a cloud stub (its config names
``remote_host`` https://ollama.com and ``remote_model``), and VOOL then listed it.

One row per surface. A fake Ollama on an ephemeral loopback port serves ``/api/pull``,
``/api/tags`` (with ``remote_host`` rows), ``/api/show`` (with ``remote_host``) and ``/api/chat``,
and records every request, so "refused before Ollama is contacted" is checked by an empty log.
A fake Ollama model store on disk holds a cloud stub under a name with no cloud suffix.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import local_ollama_inventory, policy_engine

_REAL_INVENTORY_PAYLOAD = local_ollama_inventory._ollama_models_payload

CLOUD = "gpt-oss:20b-cloud"
STUB = "mystub:latest"  # a cloud stub renamed with `ollama cp`: no cloud suffix
LOCAL = "qwen3:8b"
REMOTE = {"remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:20b"}


class _FakeOllama:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.tags: list[dict[str, Any]] = []
        self.show: dict[str, dict[str, Any]] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, {}))
                if self.path == "/api/tags":
                    self._send(200, {"models": owner.tags})
                elif self.path == "/api/version":
                    self._send(200, {"version": "0.35.1"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(("POST", self.path, body))
                if self.path == "/api/show":
                    reply = owner.show.get(str(body.get("model") or body.get("name") or ""))
                    self._send(200, reply) if reply is not None else self._send(404, {"error": "not found"})
                elif self.path == "/api/pull":
                    self._send(200, {"status": "success"})
                elif self.path in {"/api/chat", "/api/generate"}:
                    self._send(200, {"message": {"role": "assistant", "content": "cloud answer"}, "done": True})
                else:
                    self._send(404, {"error": "not found"})

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _write_store(root: Path, name: str, config: dict[str, Any]) -> None:
    """Lay out one model the way Ollama's store does: manifests/<host>/<ns>/<model>/<tag> + blob."""
    model, tag = name.split(":", 1)
    blob = json.dumps(config).encode()
    digest = hashlib.sha256(blob).hexdigest()
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    (root / "blobs" / f"sha256-{digest}").write_bytes(blob)
    manifest_dir = root / "manifests" / "registry.ollama.ai" / "library" / model
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / tag).write_text(json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                   "digest": f"sha256:{digest}", "size": len(blob)},
        "layers": [],
    }))


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ollama-models"
    _write_store(root, CLOUD, {"model_format": "", "model_family": "", **REMOTE})
    _write_store(root, STUB, {"model_format": "", "model_family": "", **REMOTE})
    _write_store(root, LOCAL, {"model_format": "gguf", "model_family": "qwen3"})
    monkeypatch.setenv("OLLAMA_MODELS", str(root))
    return root


@pytest.fixture
def ollama(monkeypatch, store):
    fake = _FakeOllama()
    monkeypatch.setenv("VOOL_RAW_OLLAMA_API_URL", fake.url)
    monkeypatch.delenv("VOOL_INSTALLED_OLLAMA_MODELS", raising=False)
    monkeypatch.delenv("VOOL_LOADED_OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload", _REAL_INVENTORY_PAYLOAD)
    fake.tags = [
        {"name": LOCAL, "model": LOCAL, "size": 5_200_000_000, "details": {"parameter_size": "8.2B"}},
        {"name": CLOUD, "model": CLOUD, "size": 384, "details": {"parameter_size": "20.9B"}, **REMOTE},
        {"name": STUB, "model": STUB, "size": 384, "details": {"parameter_size": "20.9B"}, **REMOTE},
        # A remote row that does not even carry a cloud name in Ollama's listing.
        {"name": "other:1b", "model": "other:1b", "size": 384, "details": {}, "remote_host": "https://ollama.com"},
    ]
    fake.show = {
        LOCAL: {"template": "{{ .Prompt }}", "details": {"quantization_level": "Q4_K_M"}},
        CLOUD: {"template": "", "details": {}, **REMOTE},
        STUB: {"template": "", "details": {}, **REMOTE},
    }
    yield fake
    fake.close()


def _ollama_manifest(model_name: str, *, base_url: str = "http://127.0.0.1:11434", **metadata: Any):
    from storage.model_provider_manifest import ModelProviderManifest

    return ModelProviderManifest(
        provider_name="ollama-local",
        model_name=model_name,
        source_type="http",
        adapter_type="local_qwen_provider",
        license_name="Apache-2.0",
        license_reference="https://www.apache.org/licenses/LICENSE-2.0",
        runtime_dependency="ollama",
        runtime_config={"base_url": base_url},
        enabled=True,
        capabilities=["summarize"],
        metadata={"runtime_family": "ollama", **metadata},
    )


def _json(response) -> dict:
    return json.loads(bytes(response.body or b"{}").decode("utf-8"))


# ---------------------------------------------------------------- what counts as cloud


@pytest.mark.parametrize("name", [
    "gpt-oss:20b-cloud", "gpt-oss:120b-cloud", "glm-4.6:cloud", "kimi-k2:1t-cloud", "GPT-OSS:20B-CLOUD",
    " deepseek-v3.1:671b-cloud ", "someone/model:cloud", "qwen3-coder:480b-cloud@sha256:" + "a" * 64,
    "minimax-cloud",
])
def test_a_cloud_tag_is_a_cloud_model(name) -> None:
    from core.ollama_cloud_models import is_cloud_model_name

    assert is_cloud_model_name(name)


@pytest.mark.parametrize("name", ["qwen3:8b", "qwen3:4b", "gemma3:4b", "nomic-embed-text:latest",
                                  "cloudy:1b", "hf.co/user/repo:Q4_K_M", "localhost:5000/x/y:1"])
def test_a_local_name_is_not_a_cloud_model_by_name(name) -> None:
    from core.ollama_cloud_models import is_cloud_model_name

    assert not is_cloud_model_name(name)


def test_the_model_store_names_a_renamed_cloud_stub_and_an_unreadable_entry(store) -> None:
    from core.ollama_cloud_models import is_cloud_model, manifest_reports_remote

    assert manifest_reports_remote(STUB) and is_cloud_model(STUB)
    assert not manifest_reports_remote(LOCAL)
    assert not manifest_reports_remote("never-pulled:1b")  # no stub on this computer
    broken = store / "manifests" / "registry.ollama.ai" / "library" / "broken"
    broken.mkdir(parents=True)
    (broken / "latest").write_text("{not json")
    assert manifest_reports_remote("broken:latest")  # an unknown answer counts as cloud


# ---------------------------------------------------------------- pull paths


def test_the_shared_pull_refuses_a_cloud_name_before_contacting_ollama(ollama) -> None:
    from core.ollama_cloud_models import CloudModelRefused
    from core.web.api.runtime import ModelPullProgress, _stream_ollama_pull

    with pytest.raises(CloudModelRefused, match="Ollama cloud model"):
        _stream_ollama_pull(CLOUD, progress=ModelPullProgress(CLOUD))
    assert ollama.requests == []


def test_the_shared_pull_refuses_a_stub_the_store_names_remote(ollama) -> None:
    from core.ollama_cloud_models import CloudModelRefused
    from core.web.api.runtime import ModelPullProgress, _stream_ollama_pull

    with pytest.raises(CloudModelRefused):
        _stream_ollama_pull(STUB, progress=ModelPullProgress(STUB))
    assert ollama.requests == []


def test_the_explicit_pull_refuses_a_cloud_name_before_contacting_ollama(ollama) -> None:
    from core.web.api.runtime import ModelPullProgress, ensure_ollama_model

    progress = ModelPullProgress(CLOUD)
    ensure_ollama_model(CLOUD, progress=progress)
    assert progress.public["status"] == "failed"
    assert "Ollama cloud model" in progress.public["detail"]
    assert ollama.requests == []


def test_the_boot_pull_refuses_a_cloud_default_with_outbound_on(ollama, monkeypatch) -> None:
    from core.web.api import runtime as web_runtime

    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    monkeypatch.setattr(policy_engine, "get", lambda key, default=None: True if key == "network.outbound_enabled" else default)
    monkeypatch.setattr(web_runtime.threading, "Thread", lambda *a, **k: pytest.fail("no pull thread for a cloud model"))
    progress = web_runtime.start_ollama_model_pull(CLOUD, env={"VOOL_RAW_OLLAMA_API_URL": ollama.url})
    assert progress.public["status"] == "failed"
    assert "Ollama cloud model" in progress.public["detail"]
    assert ollama.requests == []


def test_the_settings_and_chat_offer_click_refuses_a_cloud_name_even_if_offered(ollama, monkeypatch) -> None:
    from core import local_model_pull

    local_model_pull._reset_for_tests()
    monkeypatch.setattr(local_model_pull, "_plan", lambda: ((LOCAL, CLOUD, STUB), 500.0))
    monkeypatch.setattr(policy_engine, "local_only_mode", lambda: False)
    for name in (CLOUD, STUB):
        status, payload = local_model_pull.start(name)
        assert status == 400
        assert payload["error"] == "ollama_cloud_model_refused"
        assert "VOOL does not download, list or run Ollama cloud models" in payload["message"]
    assert ollama.requests == []
    assert local_model_pull.status() is None


def test_the_offer_never_names_a_cloud_model(monkeypatch) -> None:
    from core import local_model_pull

    truth = SimpleNamespace(recommended_bundle_models=(LOCAL, CLOUD), fallback_bundle_models=("glm-4.6:cloud",),
                            free_disk_gb=100.0)
    monkeypatch.setattr("core.hardware_tier.probe_machine", lambda: SimpleNamespace(ram_gb=16.0))
    monkeypatch.setattr("core.install_recommendations.build_install_recommendation_truth", lambda probe=None: truth)
    monkeypatch.setattr(local_model_pull, "_bundle_author", lambda ram_gb: "")
    names, _free = local_model_pull._plan()
    assert names == (LOCAL,)


def test_the_api_pull_route_refuses_a_cloud_name(ollama, monkeypatch) -> None:
    from core import local_model_pull
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    local_model_pull._reset_for_tests()
    monkeypatch.setattr("core.web.api.onboarding_endpoints._guards", lambda *a, **k: None)
    monkeypatch.setattr(local_model_pull, "_plan", lambda: ((LOCAL, CLOUD), 500.0))
    response = dispatch_post(
        path="/api/models/local/pull", body={"model": CLOUD}, headers={"Content-Type": "application/json"},
        runtime=RuntimeServices(), model_name="vool", workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1",
    )
    assert response.status == 400
    assert _json(response)["error"] == "ollama_cloud_model_refused"
    assert ollama.requests == []


# ---------------------------------------------------------------- what VOOL lists


def test_the_installed_inventory_hides_every_cloud_row(ollama) -> None:
    from core.local_ollama_inventory import installed_ollama_model_inventory, installed_ollama_model_names

    assert installed_ollama_model_names(base_url=ollama.url) == (LOCAL,)
    assert [item.name for item in installed_ollama_model_inventory(base_url=ollama.url)] == [LOCAL]


def test_an_explicit_inventory_and_the_loaded_list_hide_cloud_names(monkeypatch) -> None:
    from core.local_ollama_inventory import installed_ollama_model_names, loaded_ollama_model_names

    env = {"VOOL_INSTALLED_OLLAMA_MODELS": f"{LOCAL},{CLOUD}", "VOOL_LOADED_OLLAMA_MODELS": f"{CLOUD},{LOCAL}"}
    assert installed_ollama_model_names(env=env) == (LOCAL,)
    assert loaded_ollama_model_names(env=env) == (LOCAL,)


def test_the_loaded_list_from_ollama_hides_a_remote_row(ollama, monkeypatch) -> None:
    from core.local_ollama_inventory import loaded_ollama_model_names

    monkeypatch.setattr(local_ollama_inventory, "_ollama_models_payload",
                        lambda **kwargs: [{"name": LOCAL}, {"name": STUB, **REMOTE}])
    assert loaded_ollama_model_names(base_url=ollama.url) == (LOCAL,)


def test_settings_local_models_list_shows_no_cloud_model(ollama) -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_get

    response = dispatch_get(path="/api/models/local", query={}, runtime=RuntimeServices(), model_name="vool",
                            client_host="127.0.0.1", headers={})
    assert response.status == 200
    assert [m["model_name"] for m in _json(response)["models"]] == [LOCAL]


def test_api_tags_and_v1_models_list_no_cloud_model() -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import _ollama_tag_payload, _openai_models_payload

    snapshot = {"provider_capability_truth": [
        {"provider_id": "ollama-local:qwen3:8b", "model_id": LOCAL},
        {"provider_id": "ollama-local:gpt-oss:20b-cloud", "model_id": CLOUD},
    ]}
    runtime = RuntimeServices()
    tags = [m["name"] for m in _ollama_tag_payload(capability_snapshot=snapshot, model_name=CLOUD, runtime=runtime)["models"]]
    models = [m["id"] for m in _openai_models_payload(capability_snapshot=snapshot, model_name=CLOUD)["data"]]
    for listing in (tags, models):
        assert LOCAL in listing
        assert not any("cloud" in item for item in listing)


def test_other_tag_readers_skip_cloud_rows(monkeypatch) -> None:
    from core import craft_upgrade, intent_arbiter
    from core.local_model_presence import _listed_chat_models

    rows = [{"name": CLOUD, "details": {"parameter_size": "120B"}, **REMOTE},
            {"name": STUB, "details": {"parameter_size": "120B"}, **REMOTE},
            {"name": "qwen3:0.6b", "details": {"parameter_size": "0.6B"}}]
    assert _listed_chat_models({"models": [rows[0], rows[1]]}) == []
    assert _listed_chat_models({"models": rows}) == ["qwen3:0.6b"]

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": rows}

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    assert [m["name"] for m in craft_upgrade.list_local_models("http://127.0.0.1:1")] == ["qwen3:0.6b"]
    rows[2] = {"name": "llama3.2:3b", "details": {"parameter_size": "3B"}}
    monkeypatch.setattr(intent_arbiter, "_MODEL_CACHE", {})
    monkeypatch.setattr(intent_arbiter, "_local_models_disabled", lambda: False)
    monkeypatch.setenv(intent_arbiter._MODEL_ENV, CLOUD)
    assert intent_arbiter._resolve_model() == "llama3.2:3b"


def test_the_autopilot_residency_pick_never_selects_a_cloud_model(ollama) -> None:
    from core.local_inference_autopilot import select_daily_residency_model_tag
    from core.web.api.runtime import candidate_aware_daily_runtime_model_tag

    assert select_daily_residency_model_tag([CLOUD, "glm-4.6:cloud"], default_model_tag=CLOUD) == ""
    assert select_daily_residency_model_tag([CLOUD, LOCAL], default_model_tag=CLOUD) == LOCAL
    picked = candidate_aware_daily_runtime_model_tag(LOCAL, env={"VOOL_RAW_OLLAMA_API_URL": ollama.url})
    assert picked == LOCAL


# ---------------------------------------------------------------- registry, routing, certification


@pytest.fixture
def empty_registry():
    from storage.db import get_connection
    from storage.migrations import run_migrations

    run_migrations()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM model_provider_manifests")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = get_connection()
    try:
        conn.execute("DELETE FROM model_provider_manifests")
        conn.commit()
    finally:
        conn.close()


def test_the_registry_refuses_to_register_a_cloud_model(empty_registry) -> None:
    from core.model_registry import ModelRegistry
    from core.ollama_cloud_models import CloudModelRefused

    with pytest.raises(CloudModelRefused):
        ModelRegistry().register_manifest(_ollama_manifest(CLOUD))
    with pytest.raises(CloudModelRefused):
        ModelRegistry().register_manifest(_ollama_manifest(STUB, **REMOTE))


def test_a_persisted_cloud_manifest_is_never_listed_resolved_or_ranked(empty_registry) -> None:
    from core.model_registry import ModelRegistry
    from core.model_selection_policy import ModelSelectionRequest
    from storage.model_provider_manifest import upsert_provider_manifest

    # As an earlier build could have persisted them.
    upsert_provider_manifest(_ollama_manifest(LOCAL))
    upsert_provider_manifest(_ollama_manifest(CLOUD))
    upsert_provider_manifest(_ollama_manifest(STUB, **REMOTE))
    registry = ModelRegistry()
    assert [m.model_name for m in registry.list_manifests(enabled_only=True)] == [LOCAL]
    assert registry.get_manifest("ollama-local", CLOUD) is None
    assert registry.get_manifest("ollama-local", STUB) is None
    ranked = registry.rank_manifests(ModelSelectionRequest(task_kind="summarize", output_mode="plain_text",
                                                           preferred_model=CLOUD))
    assert [m.model_name for m in ranked] == [LOCAL]
    picked = registry.select_manifest(ModelSelectionRequest(task_kind="summarize", output_mode="plain_text",
                                                            preferred_model=CLOUD))
    assert picked is None or picked.model_name == LOCAL


def test_a_boot_pin_to_a_cloud_model_registers_no_lane(empty_registry) -> None:
    from core.model_registry import ModelRegistry
    from core.runtime_provider_defaults import _ensure_local_ollama_provider

    registry = ModelRegistry()
    assert _ensure_local_ollama_provider(registry, model_tag=CLOUD, bundle_role="general") is False
    assert registry.list_manifests() == []


def test_register_installed_refuses_a_cloud_name_and_a_stored_stub(empty_registry, store) -> None:
    from core.model_registry import ModelRegistry
    from core.ollama_cloud_models import CloudModelRefused
    from core.runtime_provider_defaults import register_installed_local_model

    for name in (CLOUD, STUB):
        with pytest.raises(CloudModelRefused):
            register_installed_local_model(ModelRegistry(), model_tag=name)
    assert ModelRegistry().list_manifests() == []


def test_the_register_door_refuses_a_cloud_model_in_plain_words(store, monkeypatch) -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    monkeypatch.setattr("core.web.api.runtime.installed_ollama_model_names", lambda **kwargs: [LOCAL, CLOUD, STUB])
    monkeypatch.setattr("core.runtime_provider_defaults.register_installed_local_model",
                        lambda *a, **k: pytest.fail("a cloud model is never registered"))
    for name in (CLOUD, STUB):
        response = dispatch_post(path="/api/models/local/register", body={"model_name": name},
                                 headers={"Content-Type": "application/json"}, runtime=RuntimeServices(),
                                 model_name="vool", workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1")
        assert response.status == 400
        assert "Ollama cloud model" in _json(response)["message"]


def test_certification_refuses_a_cloud_model_before_any_request(ollama) -> None:
    from core.local_model_tool_certification import CertificationBoundaryError, run_local_model_tool_certification

    for manifest in (_ollama_manifest(CLOUD, base_url=ollama.url), _ollama_manifest(STUB, base_url=ollama.url)):
        with pytest.raises(CertificationBoundaryError, match="Ollama cloud model"):
            run_local_model_tool_certification(manifest, timeout_seconds=5)
    assert ollama.requests == []


def test_certification_refuses_a_model_whose_show_reports_remote_host(ollama, tmp_path) -> None:
    from core.local_model_tool_certification import CertificationBoundaryError, run_local_model_tool_certification

    ollama.tags = [{"name": "plain:1b", "model": "plain:1b", "digest": "abc", "details": {}}]
    ollama.show["plain:1b"] = {"template": "", "details": {}, **REMOTE}
    with pytest.raises(CertificationBoundaryError, match="Ollama cloud model"):
        run_local_model_tool_certification(_ollama_manifest("plain:1b", base_url=ollama.url), timeout_seconds=5,
                                           db_path=tmp_path / "cert.db")
    assert not any(path == "/api/chat" for _m, path, _b in ollama.requests)


def test_the_certification_door_refuses_a_cloud_model(store, monkeypatch) -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    monkeypatch.setattr("core.local_model_tool_certification.run_local_model_tool_certification",
                        lambda *a, **k: pytest.fail("a cloud model is never certified"))
    response = dispatch_post(path="/api/model-tool-certification/run",
                             body={"provider_name": "ollama-local", "model_name": CLOUD},
                             headers={"Content-Type": "application/json"}, runtime=RuntimeServices(),
                             model_name="vool", workspace_root_provider=lambda: "/tmp", client_host="127.0.0.1")
    assert response.status == 400
    assert "Ollama cloud model" in _json(response)["message"]


def test_a_cloud_manifest_is_never_a_certified_author() -> None:
    from core.final_answer_authorship import local_manifest_authorship_certified

    assert local_manifest_authorship_certified(_ollama_manifest(CLOUD)) is False
    assert local_manifest_authorship_certified(_ollama_manifest(STUB, **REMOTE)) is False


# ---------------------------------------------------------------- running and chat


def _adapter(model_name: str, base_url: str, **metadata: Any):
    from adapters.openai_compatible_adapter import OpenAICompatibleAdapter

    return OpenAICompatibleAdapter(SimpleNamespace(
        provider_id=f"ollama-local:{model_name}", provider_name="ollama-local", model_name=model_name,
        metadata={"runtime_family": "ollama", **metadata},
        runtime_config={"base_url": base_url, "timeout_seconds": 5.0,
                        "prewarm": {"strategy": "ollama_chat"}},
    ))


def test_the_adapter_never_sends_a_cloud_model_to_ollama(ollama) -> None:
    from adapters.base_adapter import ModelRequest
    from core.ollama_cloud_models import CloudModelRefused

    request = ModelRequest(task_kind="chat", prompt="hi", messages=[{"role": "user", "content": "hi"}])
    for adapter in (_adapter(CLOUD, ollama.url), _adapter(STUB, ollama.url, **REMOTE)):
        with pytest.raises(CloudModelRefused, match="Ollama cloud model"):
            adapter.run_text_task(request)
        with pytest.raises(CloudModelRefused):
            adapter.stream_text_task(request)
        with pytest.raises(CloudModelRefused):
            adapter.tool_certification_exchange(messages=[{"role": "user", "content": "x"}], tools=(),
                                                tool_choice=None, max_output_tokens=16, timeout_seconds=3)
        assert adapter.prewarm()["ok"] is False
    assert ollama.requests == []


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions", "/api/generate"])
def test_a_chat_that_names_a_cloud_model_is_refused_in_plain_words(path) -> None:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    def never(*args, **kwargs):
        raise AssertionError("no turn runs for an Ollama cloud model")

    body = {"model": CLOUD, "messages": [{"role": "user", "content": "hello"}], "prompt": "hello", "stream": False}
    response = dispatch_post(path=path, body=body, headers={"Content-Type": "application/json"},
                             runtime=RuntimeServices(), model_name="vool", workspace_root_provider=lambda: "/tmp",
                             run_agent_provider=never, stream_agent_with_events_provider=never,
                             client_host="127.0.0.1")
    assert response.status == 400
    payload = _json(response)
    assert payload["reason"] == "ollama_cloud_model_refused"
    assert payload["message"] == (
        "gpt-oss:20b-cloud is an Ollama cloud model: it runs on ollama.com, not on this computer. "
        "VOOL does not download, list or run Ollama cloud models."
    )


def test_the_bundled_serve_keeps_ollama_no_cloud_as_a_second_layer() -> None:
    from installer.bundle import bundle_supervisor

    assert bundle_supervisor._OLLAMA_CHILD_ENV.get("OLLAMA_NO_CLOUD") == "1"
