from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tools.windows_lifecycle_launcher import LaunchRefused, build_launch_spec, launch, main


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "lifecycle-run"
    bundle_root = run_root / "bundle"
    for relative in (
        "native/VOOL.exe",
        "bundle_manifest.json",
        "app/apps/vool_api_server.py",
        "bundle_supervisor.py",
        "vool_window.py",
        "python/python.exe",
        "python/pythonw.exe",
        "ollama/ollama.exe",
        "ollama/lib/ollama/llama-server.exe",
    ):
        path = bundle_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    return bundle_root, run_root


def test_launcher_builds_a_scrubbed_isolated_local_only_environment(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)
    spec = build_launch_spec(
        bundle_root,
        run_root,
        env={
            "LOCALAPPDATA": str(tmp_path / "operator-localappdata"),
            "OPENAI_API_KEY": "must-not-cross-the-launch-boundary",
            "VOOL_REMOTE_API_KEY": "must-not-cross-the-launch-boundary",
            "NULLA_REGISTER_INSTALLED_OLLAMA_MODELS": "1",
        },
    )

    assert spec.environment["VOOL_HOME"] == str((run_root / "vool-home").resolve())
    assert spec.environment["VOOL_WORKSPACE_ROOT"] == str((run_root / "workspace").resolve())
    assert spec.environment["OLLAMA_MODELS"] == str((run_root / "vool-home" / "models").resolve())
    assert spec.environment["OLLAMA_NO_CLOUD"] == "true"
    assert spec.environment["VOOL_INSTALL_PROFILE"] == "local-only"
    assert "OPENAI_API_KEY" not in spec.environment
    assert "VOOL_REMOTE_API_KEY" not in spec.environment
    assert "NULLA_REGISTER_INSTALLED_OLLAMA_MODELS" not in spec.environment
    assert not (run_root / "vool-home").exists()


def test_launcher_overrides_inherited_normal_profile(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)
    spec = build_launch_spec(
        bundle_root,
        run_root,
        env={
            "LOCALAPPDATA": str(tmp_path / "operator-localappdata"),
            "VOOL_HOME": str(tmp_path / "normal" / "VOOL"),
            "OLLAMA_MODELS": str(tmp_path / "normal" / "models"),
        },
    )
    assert spec.environment["VOOL_HOME"] == str((run_root / "vool-home").resolve())
    assert spec.environment["OLLAMA_MODELS"] == str((run_root / "vool-home" / "models").resolve())


def test_launcher_refuses_a_port_before_process_creation(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)
    calls: list[object] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("native process creation must not be reached")

    with pytest.raises(LaunchRefused, match="ports"):
        launch(
            bundle_root,
            run_root,
            env={"LOCALAPPDATA": str(tmp_path / "operator-localappdata")},
            port_probe=lambda: False,
            popen_factory=fake_popen,
        )
    assert calls == []
    assert not (run_root / "vool-home").exists()


def test_launcher_requires_the_complete_staged_bundle(tmp_path: Path) -> None:
    run_root = tmp_path / "lifecycle-run"
    bundle_root = run_root / "bundle"
    (bundle_root / "native").mkdir(parents=True)
    (bundle_root / "native" / "VOOL.exe").write_bytes(b"test")

    with pytest.raises(LaunchRefused, match="staged bundle is incomplete"):
        build_launch_spec(bundle_root, run_root, env={"LOCALAPPDATA": str(tmp_path / "localappdata")})


def test_native_spawn_is_after_guard_and_uses_only_the_verified_environment(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    spec, process = launch(
        bundle_root,
        run_root,
        env={"LOCALAPPDATA": str(tmp_path / "operator-localappdata"), "OPENAI_API_KEY": "secret"},
        popen_factory=fake_popen,
    )

    assert process.pid == 1234
    assert captured["command"] == [str((bundle_root / "native" / "VOOL.exe").resolve())]
    child_env = captured["kwargs"]["env"]
    assert child_env["VOOL_HOME"] == str((run_root / "vool-home").resolve())
    assert child_env["OLLAMA_NO_CLOUD"] == "true"
    assert "OPENAI_API_KEY" not in child_env
    assert (run_root / "vool-home").is_dir()
    assert spec.native_executable == (bundle_root / "native" / "VOOL.exe").resolve()


def test_cli_check_mode_never_launches_or_creates_runtime_directories(tmp_path: Path, capsys) -> None:
    bundle_root, run_root = _stage(tmp_path)
    with mock.patch("tools.windows_lifecycle_launcher.launch") as launch_mock, mock.patch(
        "tools.windows_lifecycle_launcher.subprocess.Popen"
    ) as popen_mock:
        assert main(
            [
                "check",
                "--bundle-root",
                str(bundle_root),
                "--run-root",
                str(run_root),
            ]
        ) == 0
    launch_mock.assert_not_called()
    popen_mock.assert_not_called()
    assert not (run_root / "vool-home").exists()
    assert '"directories_created": false' in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["invalid"], ["--check-only"]])
def test_cli_missing_or_invalid_action_never_launches(tmp_path: Path, argv: list[str]) -> None:
    bundle_root, run_root = _stage(tmp_path)
    with mock.patch("tools.windows_lifecycle_launcher.subprocess.Popen") as popen_mock:
        with pytest.raises(SystemExit):
            main(
                argv
                if argv != ["invalid"]
                else [
                    "invalid",
                    "--bundle-root",
                    str(bundle_root),
                    "--run-root",
                    str(run_root),
                ]
            )
    popen_mock.assert_not_called()
    assert not (run_root / "vool-home").exists()


def test_cli_launch_with_failed_guard_never_reaches_process_creation(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)
    with mock.patch(
        "tools.windows_lifecycle_launcher.launch",
        side_effect=LaunchRefused("loopback ports are occupied"),
    ) as launch_mock, mock.patch("tools.windows_lifecycle_launcher.subprocess.Popen") as popen_mock:
        assert main(
            [
                "launch",
                "--bundle-root",
                str(bundle_root),
                "--run-root",
                str(run_root),
            ]
        ) == 2
    launch_mock.assert_called_once()
    popen_mock.assert_not_called()


def test_cli_explicit_launch_invokes_launcher_exactly_once(tmp_path: Path) -> None:
    bundle_root, run_root = _stage(tmp_path)

    class FakeProcess:
        pid = 9001

    spec = build_launch_spec(
        bundle_root,
        run_root,
        env={"LOCALAPPDATA": str(tmp_path / "operator-localappdata")},
    )
    with mock.patch(
        "tools.windows_lifecycle_launcher.launch", return_value=(spec, FakeProcess())
    ) as launch_mock, mock.patch("tools.windows_lifecycle_launcher.subprocess.Popen") as popen_mock:
        assert main(
            [
                "launch",
                "--bundle-root",
                str(bundle_root),
                "--run-root",
                str(run_root),
            ]
        ) == 0
    launch_mock.assert_called_once_with(bundle_root, run_root)
    popen_mock.assert_not_called()
