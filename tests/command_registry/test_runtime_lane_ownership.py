"""Named runtime lanes do not silently absorb new actions or weaken their guards."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from core.command_registry.census import CensusItem, classify_all, load_pin, run_census
from core.web.api.runtime import ModelPullProgress, RuntimeServices
from core.web.api.service import dispatch_post

OWNERS = {
    "http:GET:/companion": "core.companion_world_fragment.render_desktop_companion_html",
    "http:POST:/api/runtime/model-pull/retry": "RuntimeServices.model_pull_restart",
    "http:POST:/api/runtime/local-model/retry": "RuntimeServices.local_model_setup_refresh",
}
RETRIES = (
    "/api/runtime/model-pull/retry",
    "/api/runtime/local-model/retry",
)


@pytest.fixture(scope="module")
def rows():
    return {item.census_id: item for item in run_census().items}


@pytest.mark.parametrize("census_id,owner", OWNERS.items())
def test_runtime_lanes_name_their_real_owner_without_claiming_registry_migration(rows, census_id, owner):
    item = rows[census_id]
    assert item.classification == "EXTERNAL_TRANSPORT_ONLY"
    assert owner in item.note
    assert item.registry_command_id == ""
    assert item.effects == item.permission == ""
    if census_id.startswith("http:POST:"):
        assert "operator action" in item.note
        assert "loopback" in item.note and "same-origin" in item.note
        assert "read_only" not in item.note


@pytest.mark.parametrize("census_id", OWNERS)
def test_runtime_lane_pin_matches_the_discovered_exception(rows, census_id):
    entry = load_pin()["items"][census_id]
    assert entry["classification"] == rows[census_id].classification
    assert entry["note"] == rows[census_id].note
    assert entry["registry_command_id"] == ""


@pytest.mark.parametrize("census_id", OWNERS)
def test_removing_an_exact_runtime_exception_restores_the_ownership_failure(monkeypatch, rows, census_id):
    from core.command_registry.census import OVERRIDES

    monkeypatch.delitem(OVERRIDES, census_id, raising=False)
    item = classify_all([rows[census_id]], set())[0]
    assert item.classification == "LEGACY_UNMIGRATED"


@pytest.mark.parametrize("method,path", [
    ("POST", "/companion"),
    ("POST", "/api/runtime/model-pull/delete"),
    ("POST", "/api/runtime/model-pull/retry/extra"),
    ("POST", "/api/runtime/local-model/replace"),
    ("POST", "/api/runtime/local-model/retry/extra"),
])
def test_runtime_exceptions_do_not_hide_nearby_actions(method, path):
    census_id = f"http:{method}:{path}"
    item = classify_all([CensusItem(census_id, "http", f"{method} {path}")], set())[0]
    assert item.classification == "LEGACY_UNMIGRATED"


def _runtime():
    pull = ModelPullProgress("synthetic-selected-model")
    pull.update(status="ready", percent=100, detail="complete")
    return RuntimeServices(
        model_pull=pull,
        local_model_setup={"state": "failed", "chat_ready": False},
        local_models_enabled_check=lambda: True,
        model_pull_restart=Mock(return_value=(202, {"ok": True})),
        local_model_setup_refresh=Mock(return_value=True),
        local_model_status_refresh=Mock(),
    )


def _post(path, runtime, *, body=None, headers=None, host="127.0.0.1"):
    return dispatch_post(
        path=path, body={} if body is None else body,
        headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:11435"} if headers is None else headers,
        runtime=runtime, model_name="vool", workspace_root_provider=lambda: ".", client_host=host,
    )


@pytest.mark.parametrize("path", RETRIES)
@pytest.mark.parametrize("kwargs,status", [
    ({"host": "192.0.2.20"}, 403),
    ({"headers": {"Content-Type": "application/json", "Origin": "https://untrusted.example"}}, 403),
    ({"headers": {"Content-Type": "text/plain"}}, 415),
    ({"body": {"model": "different-model"}}, 400),
    ({"body": {"base_url": "https://untrusted.example"}}, 400),
])
def test_retry_transport_refusals_never_reach_any_runtime_callback(path, kwargs, status):
    runtime = _runtime()
    response = _post(path, runtime, **kwargs)
    assert response.status == status
    runtime.model_pull_restart.assert_not_called()
    runtime.local_model_setup_refresh.assert_not_called()
    runtime.local_model_status_refresh.assert_not_called()


@pytest.mark.parametrize("status,payload", [
    (202, {"ok": True, "model_pull": {"status": "pulling"}}),
    (409, {"ok": False, "error": "local models are disabled by policy"}),
    (429, {"ok": False, "error": "wait briefly before retrying"}),
])
def test_download_retry_preserves_the_owning_callback_status_and_payload(status, payload):
    runtime = _runtime()
    runtime.model_pull_restart.return_value = (status, payload)
    response = _post(RETRIES[0], runtime)
    assert response.status == status
    assert json.loads(response.body) == payload
    runtime.model_pull_restart.assert_called_once_with()
    runtime.local_model_setup_refresh.assert_not_called()


@pytest.mark.parametrize("state,policy,pull_status", [
    ("failed", False, "ready"),
    ("failed", True, "pulling"),
    ("certifying", True, "ready"),
])
def test_local_check_retry_cannot_start_a_download_or_cross_its_state_and_policy_gate(state, policy, pull_status):
    runtime = _runtime()
    runtime.local_models_enabled_check = lambda: policy
    runtime.local_model_setup["state"] = state
    runtime.model_pull.update(status=pull_status)
    response = _post(RETRIES[1], runtime)
    assert response.status == 409
    runtime.model_pull_restart.assert_not_called()
    runtime.local_model_setup_refresh.assert_not_called()


def test_local_check_retry_calls_only_its_own_selected_runtime_owner():
    runtime = _runtime()
    response = _post(RETRIES[1], runtime)
    assert response.status == 202
    assert json.loads(response.body)["ok"] is True
    runtime.local_model_setup_refresh.assert_called_once_with()
    runtime.model_pull_restart.assert_not_called()
