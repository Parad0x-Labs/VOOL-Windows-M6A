from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from core.auto_local_only_mode import cloud_lane_permitted
from core.runtime_bootstrap import resolve_backend_selection, runtime_provider_decision
from core.runtime_context import build_runtime_context, current_local_only_mode
from core.runtime_install_profiles import active_install_profile_id, persist_install_profile_record
from core.web.api.runtime import RuntimeServices, bind_runtime_provider_policy
from core.web.api.service import capability_snapshot_with_runtime
from installer.bundle.bundle_supervisor import BundleSupervisor


def test_fresh_native_bundle_enforces_local_only_through_boot_and_turn(tmp_path):
    supervisor = BundleSupervisor(tmp_path / "bundle", env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor._prepare()
    with mock.patch("core.policy_engine.local_only_mode", return_value=False):
        context = build_runtime_context(mode="api_server", env=supervisor.env)
        assert current_local_only_mode(runtime_home=supervisor.home, env=supervisor.env)
    assert context.feature_flags.local_only_mode is True
    assert context.feature_flags.allow_remote_only_without_backend is False

    manager = mock.Mock()
    manager.detect_hardware.return_value = {}
    manager.select_backend.return_value = SimpleNamespace(backend_name="NoBackend", device="none", reason="missing")
    manager.healthcheck.return_value = False
    selection = resolve_backend_selection(
        manager=manager, local_only=context.feature_flags.local_only_mode, allow_remote_only=True,
    )
    boot = runtime_provider_decision(selection, local_only=True, allow_remote_only=True)
    runtime = RuntimeServices(
        local_only_mode=True,
        runtime_home=str(supervisor.home),
        runtime_version_stamp={"boot_provider_decision": dict(boot), "provider_decision": dict(boot)},
        local_model_setup={"chat_ready": False},
    )
    payload = capability_snapshot_with_runtime(runtime, lambda: {"capabilities": []})
    assert payload["boot_provider_decision"] == boot
    assert payload["current_provider_decision"]["effective_mode"] == "unavailable_no_local_backend"
    assert payload["current_provider_decision"]["remote_request_eligible"] is False
    assert payload["current_provider_decision"]["remote_fallback_allowed"] is False
    assert payload["current_provider_decision"]["chat_ready"] is False
    assert payload["feature_flags"]["local_only_mode"] is True
    source_context = {}
    with mock.patch.dict("os.environ", supervisor.env), mock.patch("core.policy_engine.local_only_mode", return_value=False):
        bind_runtime_provider_policy(source_context, runtime)
    assert cloud_lane_permitted(source_context) is False
    assert not (supervisor.home / "config" / "install-profile.json").exists()


@pytest.mark.parametrize("requested", ["", "auto-recommended", "unknown-profile"])
def test_windows_bundle_without_concrete_profile_fails_closed(tmp_path, requested):
    home = tmp_path / "home"
    assert active_install_profile_id(
        runtime_home=home, env={"VOOL_WINDOWS_BUNDLE": "1", "VOOL_INSTALL_PROFILE": requested},
    ) == "local-only"
    assert not home.exists()


def test_non_bundle_default_is_unchanged(tmp_path):
    assert active_install_profile_id(runtime_home=tmp_path / "home", env={}) == ""


def test_windows_bundle_preserves_explicit_and_saved_profiles(tmp_path):
    home = tmp_path / "home"
    record = persist_install_profile_record(home, "hybrid-kimi")
    original = record.read_bytes()
    env = {"VOOL_WINDOWS_BUNDLE": "1"}
    assert active_install_profile_id(runtime_home=home, env=env) == "hybrid-kimi"
    assert active_install_profile_id(
        runtime_home=home, env={**env, "VOOL_INSTALL_PROFILE": "ollama-max"},
    ) == "local-max"
    assert record.read_bytes() == original
    # A later saved owner choice, not a permanently stamped boot default, wins.
    persist_install_profile_record(home, "hybrid-tether")
    assert active_install_profile_id(runtime_home=home, env=env) == "hybrid-tether"


def test_windows_bundle_honors_saved_provider_env_profile(tmp_path):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "provider-env.sh").write_text("export VOOL_INSTALL_PROFILE=hybrid-kimi\n", encoding="utf-8")
    assert active_install_profile_id(runtime_home=home, env={"VOOL_WINDOWS_BUNDLE": "1"}) == "hybrid-kimi"
