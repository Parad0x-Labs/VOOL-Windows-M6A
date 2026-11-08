"""GET /api/runtime/capabilities states the Windows limits, each read from the gate that enforces it.

The installed Windows 0.7.1-beta answered this endpoint with `allow_sandbox_execution: true` and no
word about what Windows refuses. These tests switch each Windows gate on through its own test switch
(never `os.name`) and check that the snapshot reports what that gate does, so a change to a gate
changes the report with it, and that a Mac/Linux snapshot is unchanged.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from core import plugin_executor, windows_path_policy, windows_write_gate
from core.artifact_readers import _sandbox as reader_sandbox
from core.hardware_tier import MachineProbe, QwenTier
from core.provider_routing import ProviderCapabilityTruth
from core.runtime_backbone import ProviderRegistrySnapshot
from core.runtime_capabilities import runtime_capability_snapshot
from core.runtime_context import RuntimeContext, RuntimeFeatureFlags, RuntimePaths
from core.runtime_install_profiles import InstallProfileTruth
from sandbox import job_runner, windows_confinement
from sandbox.job_runner import JobRunner, KernelIsolationUnavailableError

FAKE_LAUNCHER = Path("C:/Program Files/VOOL/broker/confine/VoolConfine.exe")


def _context(*, allow_sandbox_execution: bool = True) -> RuntimeContext:
    flags = RuntimeFeatureFlags(
        local_only_mode=True,
        public_hive_enabled=True,
        helper_mesh_enabled=True,
        allow_workspace_writes=True,
        allow_sandbox_execution=allow_sandbox_execution,
        allow_remote_only_without_backend=False,
    )
    paths = RuntimePaths(
        project_root=Path("/tmp/project"),
        runtime_home=Path("/tmp/runtime"),
        data_dir=Path("/tmp/runtime/data"),
        config_home_dir=Path("/tmp/runtime/config"),
        docs_dir=Path("/tmp/project/docs"),
        project_config_dir=Path("/tmp/project/config"),
        workspace_root=Path("/tmp/project/workspace"),
        db_path=Path("/tmp/runtime/data/vool.db"),
    )
    return RuntimeContext(mode="test", paths=paths, log_level="INFO", json_logs=True, feature_flags=flags)


def _snapshot(context: RuntimeContext | None = None) -> dict:
    install_profile = InstallProfileTruth(
        profile_id="local-only",
        label="Local only",
        summary="Single local lane.",
        selection_source="auto",
        selected_model="qwen3:8b",
        provider_mix=tuple(),
        estimated_download_gb=8.0,
        estimated_disk_footprint_gb=12.0,
        minimum_free_space_gb=11.0,
        ram_expectation_gb=12.0,
        vram_expectation_gb=0.0,
        ready=True,
        degraded=False,
        single_volume_ready=True,
        reasons=tuple(),
        volume_checks=tuple(),
    )
    provider_snapshot = ProviderRegistrySnapshot(
        warnings=tuple(),
        audit_rows=tuple(),
        capability_truth=(
            ProviderCapabilityTruth(
                provider_id="local-qwen-http:qwen3:8b",
                model_id="qwen3:8b",
                role_fit="drone",
                context_window=32768,
                tool_support=("structured_json",),
                structured_output_support=True,
                tokens_per_second=12.0,
                ram_budget_gb=12.0,
                vram_budget_gb=0.0,
                quantization="Q4_K_M",
                locality="local",
                privacy_class="local_private",
                queue_depth=0,
                max_safe_concurrency=1,
                availability_state="ready",
                circuit_open=False,
                last_error=None,
            ),
        ),
    )
    with mock.patch("core.runtime_capabilities.probe_machine", return_value=MachineProbe(8, 16.0, None, None, "cpu")), \
            mock.patch("core.runtime_capabilities.select_qwen_tier", return_value=QwenTier("base", "qwen3:8b", 8.0, 0.0, 16.0)), \
            mock.patch("core.runtime_capabilities.build_provider_registry_snapshot", return_value=provider_snapshot), \
            mock.patch("core.runtime_capabilities.build_install_profile_truth", return_value=install_profile):
        return runtime_capability_snapshot(context or _context())


@pytest.fixture
def windows(monkeypatch):
    """Every Windows gate switched on through its own test switch; the launcher verified and proven."""
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_BARE", True)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(reader_sandbox, "_host_is_macos", lambda: False)
    monkeypatch.setattr(windows_write_gate, "flagged_roots", lambda environ=None: ["c:\\users\\me\\scratch"])
    monkeypatch.setattr(windows_confinement, "launcher_path", lambda project_root: FAKE_LAUNCHER)
    monkeypatch.setitem(job_runner._BACKEND_USABLE, f"voolconfine:{FAKE_LAUNCHER}", True)
    return monkeypatch


def test_windows_snapshot_names_the_programs_the_sandbox_refuses(windows) -> None:
    snapshot = _snapshot()
    sandbox = snapshot["windows_limits"]["sandbox"]

    assert sandbox["backend"] == "proven"
    assert {"python", "pytest"} <= set(sandbox["runs_programs"])
    assert sandbox["refused_programs"] == ["git", "node", "npm", "pip", "ruff"]
    assert sandbox["programs"]["python"] == "confined"
    assert sandbox["programs"]["git"] == "refused"


def test_windows_sandbox_flag_is_a_separate_field_and_the_policy_flag_is_left_alone(windows) -> None:
    snapshot = _snapshot()

    # The policy flag execution_gate reads stays the policy value: Python and pytest do run.
    assert snapshot["feature_flags"]["allow_sandbox_execution"] is True
    assert snapshot["feature_flags"]["sandbox_execution_programs"] == snapshot["windows_limits"]["sandbox"]["runs_programs"]
    row = {item["name"]: item for item in snapshot["capabilities"]}["sandbox_execution"]
    assert row["state"] == "limited_by_platform"
    assert "python" in row["reason"] and "pytest" in row["reason"]
    for refused in ("git", "npm", "node", "ruff", "pip"):
        assert refused in row["reason"]


def test_windows_snapshot_reports_plugins_and_mcp_as_not_started_unconfined(windows) -> None:
    limits = _snapshot()["windows_limits"]

    assert limits["plugins"]["status"] == "refused"
    assert limits["mcp_servers"]["status"] == "refused_unless_confined_program"
    assert limits["mcp_servers"]["proven"] is False


def test_a_plugin_handler_is_refused_by_the_phase1_gate_the_report_relies_on(tmp_path) -> None:
    handler = tmp_path / "plugins" / "demo" / "handler.py"
    handler.parent.mkdir(parents=True)
    handler.write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(KernelIsolationUnavailableError):
        JobRunner._windows_phase1_argv([str(handler)])


def test_windows_snapshot_reports_writes_only_in_marked_folders(windows) -> None:
    snapshot = _snapshot()
    writes = snapshot["windows_limits"]["writes"]

    assert writes["write_scope"] == "marked_folders_only"
    assert writes["marked_folders"] == ["c:\\users\\me\\scratch"]
    assert writes["env_var"] == "VOOL_WINDOWS_DISPOSABLE_WORKSPACES"
    assert "operator.cleanup_temp_files" in writes["always_refused_intents"]
    tools = snapshot["workspace_access"]["workspace_tools"]
    assert tools["write_scope"] == "marked_folders_only"
    assert tools["marked_folders"] == ["c:\\users\\me\\scratch"]


def test_windows_snapshot_with_no_marked_folder_says_every_write_is_refused(windows) -> None:
    windows.setattr(windows_write_gate, "flagged_roots", lambda environ=None: [])

    writes = _snapshot()["windows_limits"]["writes"]

    assert writes["marked_folders"] == []
    assert "every such write is refused" in writes["reason"]


def test_windows_snapshot_lists_pdf_and_7z_as_not_read(windows) -> None:
    reading = _snapshot()["windows_limits"]["file_reading"]

    not_read = {row["format"] for row in reading["not_read"]}
    assert {"pdf", "7z"} <= not_read
    assert reading["confinement_available"] is False


def test_windows_snapshot_reports_share_and_device_paths_refused(windows) -> None:
    paths = _snapshot()["windows_limits"]["non_local_paths"]

    assert paths["refused"] is True
    assert paths["status"] == windows_path_policy.STATUS


def test_the_refused_list_follows_the_phase1_gate_not_a_copy(windows) -> None:
    windows.setattr(job_runner, "_WINDOWS_PYTEST_NAMES", frozenset())

    sandbox = _snapshot()["windows_limits"]["sandbox"]

    assert sandbox["programs"]["pytest"] == "refused"
    assert "pytest" in sandbox["refused_programs"]
    assert "pytest" not in sandbox["runs_programs"]


def test_windows_without_a_verified_launcher_reports_nothing_runs(windows) -> None:
    windows.setattr(windows_confinement, "launcher_path", lambda project_root: None)

    snapshot = _snapshot()

    assert snapshot["windows_limits"]["sandbox"]["backend"] == "launcher_missing"
    assert snapshot["windows_limits"]["sandbox"]["runs_programs"] == []
    assert snapshot["feature_flags"]["sandbox_execution_programs"] == []
    row = {item["name"]: item for item in snapshot["capabilities"]}["sandbox_execution"]
    assert row["state"] == "unavailable_on_host"


def test_windows_launcher_that_failed_its_probe_reports_nothing_runs(windows) -> None:
    windows.setitem(job_runner._BACKEND_USABLE, f"voolconfine:{FAKE_LAUNCHER}", False)

    sandbox = _snapshot()["windows_limits"]["sandbox"]

    assert sandbox["backend"] == "probe_failed"
    assert sandbox["runs_programs"] == []


def test_windows_report_never_runs_the_probe_and_says_not_probed(windows) -> None:
    windows.delitem(job_runner._BACKEND_USABLE, f"voolconfine:{FAKE_LAUNCHER}")
    windows.setattr(windows_confinement, "probe", mock.Mock(side_effect=AssertionError("probe ran")))

    snapshot = _snapshot()

    assert snapshot["windows_limits"]["sandbox"]["backend"] == "not_probed"
    row = {item["name"]: item for item in snapshot["capabilities"]}["sandbox_execution"]
    assert row["state"] == "limited_by_platform"


def test_windows_sandbox_disabled_by_policy_stays_disabled_by_policy(windows) -> None:
    snapshot = _snapshot(_context(allow_sandbox_execution=False))

    row = {item["name"]: item for item in snapshot["capabilities"]}["sandbox_execution"]
    assert row["state"] == "disabled_by_policy"
    assert snapshot["feature_flags"]["allow_sandbox_execution"] is False


def test_mac_and_linux_snapshot_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_BARE", False)
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", False)

    snapshot = _snapshot()

    assert "windows_limits" not in snapshot
    assert set(snapshot["feature_flags"]) == {
        "local_only_mode",
        "public_hive_enabled",
        "helper_mesh_enabled",
        "allow_workspace_writes",
        "allow_sandbox_execution",
        "allow_remote_only_without_backend",
    }
    assert set(snapshot["workspace_access"]["workspace_tools"]) == {"status", "read", "write", "workspace_root"}
    row = {item["name"]: item for item in snapshot["capabilities"]}["sandbox_execution"]
    assert row["state"] == "implemented"
    assert row["reason"] == "Sandboxed command execution is enabled by runtime policy."
