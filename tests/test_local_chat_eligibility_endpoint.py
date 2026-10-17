"""Local-chat load admission follows the canonical endpoint without bypassing gates."""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.local_chat_eligibility import evaluate_local_chat_eligibility
from storage.model_provider_manifest import ModelProviderManifest

_TAG = "qwen2.5:7b"
_PROVIDER = "ollama-local:" + _TAG
_ENDPOINT_ENV = ("VOOL_RAW_OLLAMA_API_URL", "OLLAMA_HOST", "VOOL_OLLAMA_URL")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    connect = Mock(side_effect=AssertionError("eligibility must not open a socket"))
    create = Mock(side_effect=AssertionError("eligibility must not open a socket"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create)
    yield
    connect.assert_not_called()
    create.assert_not_called()


@pytest.fixture
def probe(tmp_path):
    def invoke(**overrides):
        calls = []
        residents = []
        target_plan = overrides.pop("plan_loader", lambda *_args, **_kwargs: SimpleNamespace(ok=False, actions=[]))

        def plan(model, **kwargs):
            calls.append({"model": model, **kwargs})
            return target_plan(model, **kwargs)

        def resident_rows():
            residents.append("checked")
            return []

        params = {
            "registry": SimpleNamespace(list_manifests=lambda **_kwargs: []),
            "model_tag": _TAG,
            "manifest": ModelProviderManifest(
                provider_name="ollama-local", model_name=_TAG, source_type="http",
                adapter_type="local_qwen_provider", enabled=True, runtime_config={},
                metadata={"deployment_class": "local", "model_digest": "sha256:synthetic"},
            ),
            "setup": {"inventory_state": "present", "certification_state": "verified"},
            "provider_capability_truth": (
                {"provider_id": _PROVIDER, "availability_state": "ready", "hardware_fit": True},
            ),
            "env": {"VOOL_LOCAL_MODELS_ENABLED": "1", "OLLAMA_HOST": "127.0.0.1:9"},
            "runtime_home": str(tmp_path),
            "runtime_identity": "synthetic-endpoint-owner",
            "inventory": (SimpleNamespace(name=_TAG),),
            "residents_loader": resident_rows,
            "plan_loader": plan,
        }
        params.update(overrides)
        return evaluate_local_chat_eligibility(**params), calls, residents

    return invoke


@pytest.mark.parametrize(("env", "process_env", "expected"), (
    ({"OLLAMA_HOST": "127.0.0.1:9"}, {}, "http://127.0.0.1:9"),
    ({"VOOL_OLLAMA_URL": "http://127.0.0.1:8/"}, {}, "http://127.0.0.1:8"),
    ({"VOOL_RAW_OLLAMA_API_URL": "http://127.0.0.1:7/", "OLLAMA_HOST": "127.0.0.1:9",
      "VOOL_OLLAMA_URL": "http://127.0.0.1:8"}, {}, "http://127.0.0.1:7"),
    ({}, {"OLLAMA_HOST": "127.0.0.1:9"}, "http://127.0.0.1:9"),
    ({"OLLAMA_HOST": " "}, {"OLLAMA_HOST": "127.0.0.1:9"}, "http://127.0.0.1:9"),
    ({"VOOL_OLLAMA_URL": "127.0.0.1:8"}, {"VOOL_RAW_OLLAMA_API_URL": "127.0.0.1:7"},
     "http://127.0.0.1:8"),
    ({}, {}, "http://127.0.0.1:11434"),
))
def test_missing_explicit_endpoint_uses_canonical_resolution(probe, monkeypatch, env, process_env, expected):
    for key, value in process_env.items():
        monkeypatch.setenv(key, value)
    result, calls, residents = probe(env={"VOOL_LOCAL_MODELS_ENABLED": "1", **env})
    assert result.endpoint == expected
    assert calls == [{"model": _TAG, "provider_id": _PROVIDER, "model_digest": "sha256:synthetic",
                      "endpoint": expected, "runtime_identity": "synthetic-endpoint-owner"}]
    assert residents == ["checked"]
    assert not result.eligible
    assert result.state == "model_load_gated_low_memory"
    assert result.load_admission == "refused"


@pytest.mark.parametrize(("explicit", "setup_url", "manifest_url", "expected"), (
    ("http://127.0.0.1:6", "http://127.0.0.1:5", "http://127.0.0.1:4", "http://127.0.0.1:6"),
    (None, "http://127.0.0.1:5", "http://127.0.0.1:4", "http://127.0.0.1:5"),
    (None, None, "http://127.0.0.1:4", "http://127.0.0.1:4"),
))
def test_explicit_endpoint_precedence_is_preserved(probe, explicit, setup_url, manifest_url, expected):
    manifest = ModelProviderManifest(
        provider_name="ollama-local", model_name=_TAG, source_type="http",
        adapter_type="local_qwen_provider", enabled=True, runtime_config={"base_url": manifest_url},
        metadata={"deployment_class": "local", "model_digest": "sha256:synthetic"},
    )
    result, calls, _residents = probe(
        manifest=manifest, endpoint=explicit,
        setup={"inventory_state": "present", "certification_state": "verified", "ollama_endpoint": setup_url},
    )
    assert result.endpoint == expected
    assert calls[0]["endpoint"] == expected
    assert len(calls) == 1
    assert not result.eligible
    assert result.load_admission == "refused"


@pytest.mark.parametrize(("gate", "state"), (
    ("disabled", "policy_disabled"), ("no_model", "model_missing"),
    ("missing_inventory", "model_missing"), ("missing_manifest", "route_manifest_unavailable"),
    ("uncertified", "certification_not_verified"), ("unhealthy", "provider_unhealthy"),
))
def test_earlier_gates_never_reach_residency_or_load_admission(probe, gate, state):
    overrides = {
        "disabled": {"env": {"VOOL_LOCAL_MODELS_ENABLED": "0", "OLLAMA_HOST": "127.0.0.1:9"}},
        "no_model": {"model_tag": ""},
        "missing_inventory": {"inventory": ()},
        "missing_manifest": {"manifest": None},
        "uncertified": {"setup": {"inventory_state": "present", "certification_state": "unknown"}},
        "unhealthy": {"provider_capability_truth": ({"provider_id": _PROVIDER, "availability_state": "unavailable"},)},
    }[gate]
    result, calls, residents = probe(**overrides)
    assert result.state == state
    assert not result.eligible
    assert calls == []
    assert residents == []


def test_a_resolved_endpoint_never_overrides_the_hardware_fence(probe):
    result, calls, _residents = probe(
        provider_capability_truth=({"provider_id": _PROVIDER, "availability_state": "ready", "hardware_fit": False},),
        plan_loader=lambda *_args, **_kwargs: SimpleNamespace(ok=True, actions=[]),
    )
    assert calls[0]["endpoint"] == "http://127.0.0.1:9"
    assert not result.eligible
    assert result.hardware_fit is False
    assert result.load_admission == "refused"


def test_a_resolved_endpoint_keeps_governor_errors_fail_closed(probe):
    def broken_governor(*_args, **_kwargs):
        raise RuntimeError("synthetic governor failure")

    result, calls, _residents = probe(plan_loader=broken_governor)
    assert calls[0]["endpoint"] == "http://127.0.0.1:9"
    assert not result.eligible
    assert result.state == "model_load_gated_low_memory"
    assert result.load_admission == "unverifiable"
    assert "RuntimeError" in result.reason
