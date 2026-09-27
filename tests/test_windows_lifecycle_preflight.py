from __future__ import annotations

from pathlib import Path

import pytest

from tools.windows_lifecycle_preflight import PreflightRefused, resolve_lifecycle_paths


def test_preflight_resolves_disposable_paths_without_creating_them(tmp_path: Path) -> None:
    bundle_root = tmp_path / "future-bundle"
    run_root = tmp_path / "future-lifecycle-run"
    local_app_data = tmp_path / "operator-local-appdata"

    report = resolve_lifecycle_paths(
        bundle_root,
        run_root,
        env={"LOCALAPPDATA": str(local_app_data)},
    )

    assert report["status"] == "resolved"
    assert report["launch_ready"] is False
    assert report["bundle_root"] == str(bundle_root.resolve())
    assert report["native_executable"] == str((bundle_root / "native" / "VOOL.exe").resolve())
    assert report["vool_home"] == str((run_root / "vool-home").resolve())
    assert report["workspace_root"] == str((run_root / "workspace").resolve())
    assert report["webview2_profile"] == str((run_root / "vool-home" / "WebView2").resolve())
    assert report["temp"] == str((run_root / "temp").resolve())
    assert report["tmp"] == report["temp"]
    assert report["api_endpoint"] == "http://127.0.0.1:11435"
    assert report["ollama_endpoint"] == "http://127.0.0.1:11434"
    assert report["directories_created"] is False
    assert not bundle_root.exists()
    assert not run_root.exists()


def test_preflight_refuses_c_nulla_state(tmp_path: Path) -> None:
    with pytest.raises(PreflightRefused, match=r"C:\\NULLA"):
        resolve_lifecycle_paths(tmp_path / "bundle", Path(r"C:\NULLA") / "run")


def test_preflight_refuses_default_operator_vool_home(tmp_path: Path) -> None:
    local_app_data = tmp_path / "operator-local-appdata"
    default_home = local_app_data / "VOOL"
    with pytest.raises(PreflightRefused, match="default_vool_home"):
        resolve_lifecycle_paths(
            tmp_path / "bundle",
            default_home / "lifecycle",
            env={"LOCALAPPDATA": str(local_app_data)},
        )
