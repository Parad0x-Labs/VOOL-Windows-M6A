from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from core.web.api.app import create_api_app
from core.web.api.runtime import RuntimeServices
from core.web.api.service import capability_snapshot_with_runtime
from tests.asgi_harness import asgi_request


@pytest.mark.parametrize("path", ["/healthz", "/v1/healthz", "/healthz/"])
def test_health_reads_published_status_without_refreshing_backend(path):
    stamp = {
        "pid": 4321,
        "commit_full": "a" * 40,
        "current_provider_decision": {"effective_mode": "unavailable_no_local_backend"},
    }
    refresh_model = mock.Mock()
    refresh_policy = mock.Mock()
    runtime = RuntimeServices(
        runtime_model_tag="test",
        runtime_version_stamp=stamp,
        local_only_mode=True,
        local_model_setup={"state": "failed", "chat_ready": False, "certification_state": "degraded"},
        provider_capability_truth=({"provider_id": "ollama-local:test", "availability_state": "blocked"},),
        local_model_status_refresh=refresh_model,
        provider_decision_refresh=refresh_policy,
    )
    app = create_api_app(runtime=runtime, model_name="test")

    status, _, raw = asgi_request(app, method="GET", path=path, headers={"Host": "127.0.0.1"})

    assert status == 200
    refresh_model.assert_not_called()
    refresh_policy.assert_not_called()
    payload = json.loads(raw)
    assert payload["ok"] is True  # API liveness, not model readiness.
    assert payload["runtime"]["pid"] == 4321
    assert payload["runtime"]["commit_full"] == "a" * 40
    capabilities = payload["capabilities"]
    assert capabilities["local_model_setup"] == runtime.local_model_setup
    assert capabilities["provider_capability_truth"][0]["availability_state"] == "blocked"
    assert capabilities["current_provider_decision"] == stamp["current_provider_decision"]
    assert capabilities["feature_flags"]["local_only_mode"] is True


def test_normal_capability_reads_still_refresh_model_and_policy():
    refresh_model = mock.Mock()
    refresh_policy = mock.Mock()
    runtime = RuntimeServices(
        runtime_model_tag="test",
        local_model_status_refresh=refresh_model,
        provider_decision_refresh=refresh_policy,
    )

    capability_snapshot_with_runtime(runtime, lambda: {})

    refresh_model.assert_called_once_with()
    refresh_policy.assert_called_once_with()


def test_health_meets_supervisor_deadline_while_model_status_io_is_blocked():
    release = threading.Event()
    runtime = RuntimeServices(
        runtime_model_tag="test",
        local_model_status_refresh=lambda: release.wait(5),
        provider_decision_refresh=mock.Mock(),
    )
    app = create_api_app(runtime=runtime, model_name="test")
    with ThreadPoolExecutor(max_workers=1) as worker:
        response = worker.submit(
            asgi_request, app, method="GET", path="/healthz", headers={"Host": "127.0.0.1"},
        )
        try:
            status, _, raw = response.result(timeout=1.5)
        finally:
            release.set()
    assert status == 200
    assert json.loads(raw)["ok"] is True
