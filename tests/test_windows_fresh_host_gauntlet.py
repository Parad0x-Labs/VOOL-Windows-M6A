from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_windows_fresh_host_gauntlet_covers_release_acceptance_chain() -> None:
    gauntlet = (REPO_ROOT / "tools" / "windows_fresh_host_gauntlet.ps1").read_text(encoding="utf-8")

    for marker in (
        "Install_And_Run_VOOL.ps1",
        "tests\\test_hardware_tier.py",
        "tests\\test_model_store_planner.py",
        "tests\\test_provider_probe.py",
        "tests\\test_provider_probe_contract.py",
        "tests\\test_install_script_contract.py",
        "tests\\test_install_surface_contracts.py",
        "tests\\test_local_only_policy.py",
        "installer\\provider_probe.py",
        "--benchmark",
        "--benchmark-timeout",
        "installer\\build_windows_package.ps1",
        "OpenClaw VOOL exact response",
        "OPENCLAW_VOOL_OK",
        "dist\\windows-gauntlet",
        "vool.windows_fresh_host_gauntlet.v1",
    ):
        assert marker in gauntlet


def test_windows_fresh_host_gauntlet_has_ci_safe_switches_and_cmd_wrapper() -> None:
    gauntlet = (REPO_ROOT / "tools" / "windows_fresh_host_gauntlet.ps1").read_text(encoding="utf-8")
    wrapper = (REPO_ROOT / "Test_VOOL_Windows_Gauntlet.cmd").read_text(encoding="utf-8")

    for switch in ("SkipInstall", "SkipBenchmark", "SkipPackageBuild", "RequireOpenClaw", "Json"):
        assert f"[switch]${switch}" in gauntlet

    assert "tools\\windows_fresh_host_gauntlet.ps1" in wrapper
    assert "if (-not $RequireOpenClaw)" in gauntlet
    assert "Not required by the native Windows artifact" in gauntlet


def test_windows_release_gauntlet_workflow_runs_on_windows() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "windows-release-gauntlet.yml").read_text(encoding="utf-8")

    assert "runs-on: windows-latest" in workflow
    assert 'python -m pip install -e ".[dev,companion]"' in workflow
    assert "Check native bundle contracts" in workflow
    assert "Compile native approval host from locked dependencies" in workflow
    assert "tests/test_windows_host_native_control.py" in workflow
    assert "dotnet restore installer/windows_host/VOOL.WindowsHost.csproj --locked-mode" in workflow
    assert "core/native_control.py" in workflow
    assert "python -m tools.windows_authority_audit --check" in workflow
    assert "tests/test_native_control.py tests/test_windows_authority_audit.py" in workflow
    assert "Test_VOOL_Windows_Gauntlet.cmd -SkipInstall -SkipBenchmark -SkipPackageBuild -Json" in workflow
    assert "dist/windows-gauntlet/*.json" in workflow
