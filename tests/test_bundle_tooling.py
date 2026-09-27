"""Contract checks for the self-contained Windows bundle tooling (installer/bundle).

These pin the launcher/build/packaging contract that a hand-verified build depends on, so a
later edit can't silently break the installer (which CI can't compile).
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from core import native_control
from core.runtime_install_profiles import default_ollama_models_path
from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor

_BUNDLE = Path(__file__).resolve().parent.parent / "installer" / "bundle"


def test_bundle_build_provenance_is_checkout_derived_and_fail_closed() -> None:
    script = (_BUNDLE / "build_bundle.ps1").read_text(encoding="utf-8")

    assert "SourceCommit" not in script
    assert "SourceBranch" not in script
    assert '[string]$buildSource.source_kind -ne "git"' in script
    assert "[string]$buildSource.commit_full -notmatch '^[0-9a-f]{40}$'" in script
    assert "[bool]$buildSource.dirty_state" in script
    assert "Build from a native Git checkout" in script
    assert "Bundle stage must be empty" in script
    assert "git -C $RepoRoot archive" in script
    assert '$SourceRoot = $releaseArchiveRoot' in script
    assert 'dirty_state = $false' in script
    assert "Remove-Item $pyDir -Recurse" not in script
    assert "Remove-Item $appDir -Recurse" not in script


def test_native_bundle_build_uses_the_pinned_dotnet_sdk() -> None:
    source_root = Path(__file__).resolve().parent.parent
    sdk = json.loads((source_root / "global.json").read_text(encoding="utf-8"))["sdk"]
    script = (_BUNDLE / "build_bundle.ps1").read_text(encoding="utf-8")

    assert sdk == {
        "version": "10.0.401",
        "rollForward": "disable",
        "allowPrerelease": False,
    }
    assert "Push-Location $SourceRoot" in script
    assert "& $DotNetExe publish" in script


def test_bundle_supervisor_pins_its_ollama_endpoint_to_loopback(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "user-data"
    supervisor = BundleSupervisor(
        root,
        env={
            "VOOL_HOME": str(home),
            "OLLAMA_HOST": "https://external.example:443",
            "VOOL_RAW_OLLAMA_API_URL": "https://other.example/api",
            "VOOL_OLLAMA_URL": "https://third.example/api",
        },
    )

    supervisor._prepare()

    expected = "http://127.0.0.1:11434"
    assert supervisor.env["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert supervisor.env["VOOL_RAW_OLLAMA_API_URL"] == expected
    assert supervisor.env["VOOL_OLLAMA_URL"] == expected
    from core.ollama_endpoint import ollama_base_url

    assert ollama_base_url(supervisor.env) == expected


def test_inno_separates_internal_and_release_identity() -> None:
    script = (_BUNDLE / "vool.iss").read_text(encoding="utf-8")

    assert '#define AppName "VOOL Internal Test"' in script
    assert '#define AppName "VOOL"' in script
    assert "AppId={#AppIdValue}" in script
    assert "OutputBaseFilename={#InstallerBaseName}" in script
    assert "UninstallDisplayIcon={app}\\native\\VOOL.exe" in script


def test_candidate_iso_builder_is_fail_closed() -> None:
    script = (Path(__file__).resolve().parent.parent / "tools" / "build_windows_candidate_iso.py").read_text(
        encoding="utf-8"
    )

    assert "refusing to overwrite existing ISO" in script
    assert 'choices=("NotSigned", "Valid")' in script
    assert "a release candidate must have a valid signature" in script
    assert "a non-release installer must be visibly named internal" in script
    assert '"installer_sha256"' in script
    assert 'purpose": "internal-clean-vm-packaging-test"' in script


def test_launcher_starts_server_windowless_and_delegates_open() -> None:
    cmd = (_BUNDLE / "vool-launch.cmd").read_text(encoding="utf-8")
    assert "vool_api_server.py" in cmd
    assert "--port 11435" in cmd
    assert "pythonw.exe" in cmd  # windowless server
    assert "ollama" in cmd.lower()  # starts the bundled Ollama
    assert "vool_window.py" in cmd  # opens the native WebView2 app window (not a browser tab)
    # models + data live OUTSIDE the install dir so uninstall/reinstall never deletes the LLMs/wallet
    assert "OLLAMA_MODELS=%VOOL_HOME%\\models" in cmd
    assert "OLLAMA_MODELS=%VOOL_ROOT%models" not in cmd  # not the (uninstall-deletable) install dir
    assert "bundle_supervisor.py" in cmd
    assert "bundle_manifest.json" in cmd
    assert '--root "%VOOL_ROOT:~0,-1%"' in cmd
    assert 'PYTHONPATH=%VOOL_ROOT%app;' in cmd


def test_bundle_supervisor_is_stoppable_and_persists_diagnostics() -> None:
    supervisor = (_BUNDLE / "bundle_supervisor.py").read_text(encoding="utf-8")
    assert "--stop" in supervisor
    assert "bundle-supervisor.log" in supervisor
    assert "bundle-supervisor.json" in supervisor
    assert "retry_in" in supervisor
    assert "CREATE_NO_WINDOW" in supervisor
    assert "VOOL_BUNDLE_SUPERVISOR" in supervisor and "CreateMutexW" in supervisor


def test_bundle_supervisor_bootstraps_api_imports_for_embedded_python(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home)})
    supervisor._prepare()

    with (
        mock.patch.object(supervisor, "_health_probe", return_value=False),
        mock.patch.object(supervisor, "_api_listener_pid", return_value=None),
        mock.patch.object(supervisor, "_start") as start,
    ):
        supervisor._ensure_api()

    argv = start.call_args.args[1]
    assert argv[1:3] == ["-B", "-c"]
    assert "sys.path.insert(0,app_root)" in argv[3]
    assert argv[4] == str(root / "app")
    assert argv[5].endswith("app\\apps\\vool_api_server.py")


def test_supervisor_sets_per_user_home_before_preflight(tmp_path, monkeypatch) -> None:
    import core.runtime_paths
    from core.runtime_paths import active_vool_home

    root = tmp_path / "bundle"
    (root / "app").mkdir(parents=True)
    home = tmp_path / "user-data"
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("VOOL_PROJECT_ROOT", "")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    monkeypatch.setattr(core.runtime_paths, "_VOOL_HOME_OVERRIDE", None)
    monkeypatch.setattr("sys.argv", ["bundle_supervisor.py", "--root", str(root)])
    observed = {}

    def record_preflight(_surface):
        observed["home"] = active_vool_home()
        observed["project"] = os.environ.get("VOOL_PROJECT_ROOT")

    with (
        mock.patch("core.unattended_preflight.preflight", side_effect=record_preflight),
        mock.patch.object(BundleSupervisor, "run", return_value=0),
    ):
        assert bundle_supervisor.main() == 0

    assert observed == {"home": home.resolve(), "project": str(root / "app")}
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_bundle_manifest_controls_model_store_for_supervisor_and_runtime(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "schema": "vool.bundle_manifest.v1",
                "selected_model": "qwen2.5:7b",
                "model_store": "%LOCALAPPDATA%\\\\VOOL\\\\models",
            }
        )
        + "\n",
        encoding="utf-8-sig",
    )
    home = tmp_path / "runtime"
    env = {
        "VOOL_HOME": str(home),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "VOOL_BUNDLE_ROOT": str(root),
    }
    supervisor = BundleSupervisor(root, env=env)
    supervisor._prepare()

    assert Path(supervisor.env["OLLAMA_MODELS"]) == home / "models"
    assert default_ollama_models_path(env) == (home / "models").resolve()
    assert (home / "models").is_dir()


def test_bundle_window_attaches_to_the_supervisor_owned_api(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    commit = "a" * 40
    (root / "bundle_manifest.json").write_text(
        json.dumps({"source_commit_full": commit, "selected_model": "qwen2.5:7b"}), encoding="utf-8"
    )
    supervisor = BundleSupervisor(
        root,
        env={
            "LOCALAPPDATA": str(tmp_path / "local"),
            "VOOL_PUBLIC_HIVE_ENABLED": "1",
            "VOOL_ENABLE_WINDOWS_MESH_DAEMON": "1",
        },
    )
    supervisor._prepare()

    assert supervisor.env["VOOL_EXPECTED_COMMIT"] == commit
    assert supervisor.env["VOOL_PROJECT_ROOT"] == str(root / "app")
    assert supervisor.env["VOOL_WINDOWS_BUNDLE"] == "1"
    assert supervisor.env["VOOL_DISABLE_MESH_DAEMON"] == "1"
    assert supervisor.env["VOOL_PUBLIC_HIVE_ENABLED"] == "0"
    assert supervisor.env["VOOL_RUNTIME_MODE"] == "self-contained"
    assert supervisor.env["VOOL_NATIVE_REQUIRE_OWNED_RUNTIME"] == "0"
    assert supervisor.env["VOOL_NATIVE_ATTACH_ONLY"] == "1"


def test_bundle_control_secret_uses_child_pipes_and_rotates_with_api(tmp_path) -> None:
    class CapturePipe(io.BytesIO):
        captured = b""

        def close(self) -> None:
            self.captured = self.getvalue()
            super().close()

    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor._prepare()
    children = [mock.Mock(pid=101, stdin=CapturePipe()), mock.Mock(pid=102, stdin=CapturePipe()), mock.Mock(pid=103, stdin=CapturePipe())]
    for child in children:
        child.poll.return_value = 0

    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", side_effect=children) as popen:
        supervisor._start("api", ["api"], cwd=root)
        first_secret = children[0].stdin.captured
        supervisor._start("window", ["window"], cwd=root)
        old_proof = native_control.sign_headers(first_secret, "POST", "/api/mode", b"{}")

        assert len(first_secret) == 32
        assert children[1].stdin.captured == first_secret
        for call in popen.call_args_list[:2]:
            assert call.kwargs["stdin"] == bundle_supervisor.subprocess.PIPE
            assert call.kwargs["env"]["VOOL_NATIVE_CONTROL_STDIN"] == "1"
            assert first_secret.hex() not in repr(call.kwargs)
        assert first_secret.hex() not in supervisor.state_path.read_text(encoding="utf-8")

        supervisor._stop_child("api")
        assert "window" not in supervisor.processes
        assert supervisor._control_secret is None
        supervisor._start("api", ["api"], cwd=root)
        assert children[2].stdin.captured != first_secret
        assert not native_control.NativeControlVerifier(children[2].stdin.captured).verify(
            "POST", "/api/mode", b"{}", old_proof
        )


def test_bundle_supervisor_restarts_dead_child_with_backoff_and_logs() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "bundle"
        root.mkdir()
        (root / "bundle_manifest.json").write_text(
            json.dumps({"schema": "vool.bundle_manifest.v1", "selected_model": "qwen2.5:7b"}) + "\n",
            encoding="utf-8-sig",
        )
        home = Path(tmpdir) / "home"
        supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
        supervisor._prepare()
        first = mock.Mock(pid=101)
        first.poll.return_value = None
        replacement = mock.Mock(pid=202)
        replacement.poll.return_value = None

        with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", side_effect=[first, replacement]) as popen, mock.patch.object(
            supervisor, "_health_probe", return_value=False
        ), mock.patch.object(supervisor, "_ollama_listener_pid", return_value=None):
            supervisor._ensure_ollama()
            assert popen.call_count == 1
            first.poll.return_value = 17
            supervisor._reap_dead("ollama")
            assert supervisor.failures["ollama"] == 1
            assert supervisor.next_attempt["ollama"] > 0
            supervisor.next_attempt["ollama"] = 0
            supervisor._ensure_ollama()
            assert popen.call_count == 2
            supervisor._stop_child("ollama")

        log = (home / "logs" / "bundle-supervisor.log").read_text(encoding="utf-8")
        assert "exited returncode=17" in log
        assert "retry_in=" in log
        state = json.loads((home / "run" / "bundle-supervisor.json").read_text(encoding="utf-8"))
        assert state["restart_counts"]["ollama"] == 2
        assert state["last_exit"]["ollama"]["reason"] == "exited returncode=17"


def test_bundle_supervisor_stops_the_windows_process_tree(tmp_path, monkeypatch) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervisor._prepare()
    child = mock.Mock(pid=404)
    child.poll.return_value = None
    child.wait.return_value = 0
    supervisor.processes["ollama"] = child
    monkeypatch.setattr(bundle_supervisor.os, "name", "nt")

    with mock.patch(
        "installer.bundle.bundle_supervisor.subprocess.run",
        return_value=mock.Mock(returncode=0),
    ) as run:
        supervisor._stop_child("ollama")

    assert run.call_args.args[0] == ["taskkill.exe", "/PID", "404", "/T", "/F"]
    assert run.call_args.kwargs["timeout"] == 8
    child.terminate.assert_not_called()
    child.wait.assert_called_once_with(timeout=5)


def test_bundle_supervisor_lease_prevents_duplicate_instances(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    env = {"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)}
    first = BundleSupervisor(root, env=env)
    second = BundleSupervisor(root, env=env)

    assert first._acquire_lease() is True
    try:
        assert second._acquire_lease() is False
    finally:
        first._release_lease()

    try:
        assert second._acquire_lease() is True
    finally:
        second._release_lease()


def test_bundle_supervisor_duplicate_launch_requests_window_reopen(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    env = {"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)}
    first = BundleSupervisor(root, env=env)
    second = BundleSupervisor(root, env=env)

    assert first._acquire_lease() is True
    try:
        assert second.run(once=True) == 0
        assert second.window_reopen_path.read_text(encoding="ascii").strip()
        log = (home / "logs" / "bundle-supervisor.log").read_text(encoding="utf-8")
        assert "window reopen requested" in log
    finally:
        first._release_lease()


def test_bundle_supervisor_consumes_window_reopen_and_rearms_window(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home)})
    supervisor._prepare()
    supervisor.window_reopen_path.write_text("request\n", encoding="ascii")
    supervisor.window_closed = True
    supervisor.window_started = True

    with mock.patch.object(supervisor, "_stop_child") as stop_child:
        assert supervisor._consume_window_reopen() is True

    stop_child.assert_not_called()
    assert supervisor.window_closed is False
    assert supervisor.window_started is False
    assert supervisor.next_attempt["window"] == 0.0
    assert not supervisor.window_reopen_path.exists()


def test_bundle_supervisor_startup_timeout_enters_bounded_backoff(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "bundle_manifest.json").write_text(
        json.dumps({"schema": "vool.bundle_manifest.v1", "selected_model": "qwen2.5:7b"}) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervisor._prepare()
    stalled = mock.Mock(pid=303)
    stalled.poll.return_value = None
    supervisor.processes["api"] = stalled
    supervisor.started_at["api"] = 0

    with (
        mock.patch.object(supervisor, "_healthy", return_value=False),
        mock.patch.object(supervisor, "_stop_child") as stop_child,
    ):
        supervisor._ensure_api()

    stop_child.assert_called_once_with("api")
    assert supervisor.failures["api"] == 1
    assert supervisor.next_attempt["api"] > 0
    log = (home / "logs" / "bundle-supervisor.log").read_text(encoding="utf-8")
    assert "startup timeout" in log


def test_bundle_supervisor_does_not_restart_previously_healthy_service_on_one_probe_miss(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor._prepare()
    for name, ensure in (("api", supervisor._ensure_api), ("ollama", supervisor._ensure_ollama)):
        child = mock.Mock(pid=303)
        child.poll.return_value = None
        supervisor.processes[name] = child
        supervisor.started_at[name] = 0.0
        supervisor.last_healthy_at[name] = 100.0
        checker = "_owned_api_healthy" if name == "api" else "_owned_ollama_healthy"

        with (
            mock.patch.object(supervisor, checker, return_value=False),
            mock.patch("installer.bundle.bundle_supervisor.time.time", return_value=101.0),
            mock.patch.object(supervisor, "_stop_child") as stop_child,
        ):
            ensure()

        stop_child.assert_not_called()
        assert supervisor.failures[name] == 0
        assert supervisor.unhealthy_since[name] == 101.0

        with (
            mock.patch.object(supervisor, checker, return_value=True),
            mock.patch("installer.bundle.bundle_supervisor.time.time", return_value=102.0),
        ):
            assert supervisor._health_probe(name, f"http://127.0.0.1/{name}") is True

        assert name not in supervisor.unhealthy_since

        with (
            mock.patch.object(supervisor, checker, return_value=False),
            mock.patch("installer.bundle.bundle_supervisor.time.time", return_value=200.0),
        ):
            assert supervisor._health_probe(name, f"http://127.0.0.1/{name}") is False
        assert supervisor.unhealthy_since[name] == 200.0

        with (
            mock.patch.object(supervisor, checker, return_value=False),
            mock.patch("installer.bundle.bundle_supervisor.time.time", return_value=261.0),
            mock.patch.object(supervisor, "_stop_child") as stop_child,
        ):
            ensure()

        stop_child.assert_called_once_with(name)
        assert supervisor.failures[name] == 1
        supervisor.processes.pop(name)


def test_bundle_supervisor_refuses_a_foreign_api_listener(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home)})
    supervisor._prepare()

    with mock.patch.object(supervisor, "_health_probe", return_value=False), mock.patch.object(
        supervisor, "_start"
    ) as start, mock.patch.object(supervisor, "_api_listener_pid", return_value=1234):
        supervisor._ensure_api()

    start.assert_not_called()
    assert "non-owned pid=1234" in (home / "logs" / "bundle-supervisor.log").read_text(encoding="utf-8")


def test_bundle_supervisor_accepts_only_its_own_exact_api(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home)})
    supervisor._prepare()
    commit = "a" * 40
    supervisor.env["VOOL_EXPECTED_COMMIT"] = commit
    child = mock.Mock(pid=4321)
    child.poll.return_value = None
    supervisor.processes["api"] = child
    payload = {"ok": True, "runtime": {"pid": 4321, "commit_full": commit}}

    with mock.patch.object(supervisor, "_api_listener_pid", return_value=4321), mock.patch(
        "installer.bundle.bundle_supervisor.urllib.request.urlopen",
        side_effect=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()),
    ):
        assert supervisor._owned_api_healthy() is True
        payload["runtime"]["commit_full"] = "b" * 40
        assert supervisor._owned_api_healthy() is False

    with mock.patch.object(supervisor, "_api_listener_pid", return_value=9876):
        assert supervisor._owned_api_healthy() is False


def test_bundle_supervisor_accepts_ollama_only_when_its_child_owns_listener(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor._prepare()
    child = mock.Mock(pid=4321)
    child.poll.return_value = None
    supervisor.processes["ollama"] = child

    with mock.patch.object(supervisor, "_ollama_listener_pid", return_value=4321), mock.patch.object(
        supervisor, "_healthy", return_value=True
    ):
        assert supervisor._owned_ollama_healthy() is True

    with mock.patch.object(supervisor, "_ollama_listener_pid", return_value=9876), mock.patch.object(
        supervisor, "_healthy", return_value=True
    ):
        assert supervisor._owned_ollama_healthy() is False


def test_bundle_supervisor_refuses_a_foreign_ollama_listener(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home)})
    supervisor._prepare()

    with mock.patch.object(supervisor, "_health_probe", return_value=False), mock.patch.object(
        supervisor, "_ollama_listener_pid", return_value=1234
    ), mock.patch.object(supervisor, "_start") as start:
        supervisor._ensure_ollama()

    start.assert_not_called()
    assert "Ollama port 11434 is held by non-owned pid=1234" in (
        home / "logs" / "bundle-supervisor.log"
    ).read_text(encoding="utf-8")


def test_bundle_supervisor_treats_http_error_as_unhealthy(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    response = mock.MagicMock(status=404)
    response.__enter__.return_value = response
    response.__exit__.return_value = None

    with mock.patch("installer.bundle.bundle_supervisor.urllib.request.urlopen", return_value=response):
        assert supervisor._healthy("http://127.0.0.1:11435/healthz") is False


def test_bundle_supervisor_stop_entrypoint_writes_stoppable_marker(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VOOL_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home))
    monkeypatch.setenv("VOOL_PROJECT_ROOT", "")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    monkeypatch.setattr("sys.argv", ["bundle_supervisor.py", "--stop"])

    assert bundle_supervisor.main() == 0
    assert (home / "run" / "stop").read_text(encoding="utf-8").strip() == "stop"


def test_bundle_supervisor_once_releases_lease_and_cleans_state(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "bundle_manifest.json").write_text(
        json.dumps({"schema": "vool.bundle_manifest.v1", "selected_model": "qwen2.5:7b"}) + "\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    env = {"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)}
    supervisor = BundleSupervisor(root, env=env)
    supervisor._ensure_ollama = mock.Mock()
    supervisor._ensure_api = mock.Mock()
    supervisor._ensure_window = mock.Mock()

    assert supervisor.run(once=True) == 0

    assert not supervisor.state_path.exists()
    assert json.loads(supervisor.model_status_path.read_text(encoding="utf-8"))["status"] == "stopped"
    log = (home / "logs" / "bundle-supervisor.log").read_text(encoding="utf-8")
    assert "supervisor started" in log
    assert "supervisor stopped" in log

    replacement = BundleSupervisor(root, env=env)
    try:
        assert replacement._acquire_lease() is True
    finally:
        replacement._release_lease()


def test_bundle_supervisor_quits_with_the_window_and_retries_launch_failures(tmp_path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor._prepare()
    supervisor.window_started = True
    failed = mock.Mock(pid=7001)
    failed.poll.return_value = 1
    supervisor.processes["window"] = failed
    supervisor._reap_dead("window")
    assert supervisor.window_started is False
    assert supervisor.window_closed is False
    assert supervisor.failures["window"] == 1

    supervisor.window_started = True
    closed = mock.Mock(pid=7002)
    closed.poll.return_value = 0
    supervisor.processes["window"] = closed
    supervisor._reap_dead("window")
    assert supervisor.window_closed is True


def test_native_window_host_uses_webview_and_owned_exact_runtime() -> None:
    py = (_BUNDLE / "vool_window.py").read_text(encoding="utf-8")
    assert "import webview" in py  # native WebView2 window, not a browser
    assert "create_window" in py and "http://127.0.0.1:11435" in py  # canonical local API origin
    assert 'URL = f"{_API_ORIGIN}/chat"' in py
    assert "supervisor.ensure_ready()" in py  # exact health + identity before any window
    assert "supervisor.shutdown()" in py  # owned backend child ends with native host
    assert "_fallback_to_edge" not in py  # a native failure must never open a browser
    assert "_has_webview2" in py
    assert "_single_instance" in py  # repeated launches don't stack multiple windows


def test_windows_native_failure_never_launches_a_browser(tmp_path, monkeypatch) -> None:
    from installer.bundle import vool_window

    monkeypatch.setattr(vool_window.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with mock.patch.object(vool_window.ctypes, "windll", create=True), mock.patch.object(
        vool_window.subprocess, "Popen"
    ) as popen:
        assert vool_window._no_native_window("WebView2 unavailable") == 1
    popen.assert_not_called()


def test_fixed_webview2_runtime_is_detected_without_registry(tmp_path, monkeypatch) -> None:
    from installer.bundle import vool_window

    runtime = tmp_path / "webview2"
    runtime.mkdir()
    monkeypatch.setenv("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", str(runtime))
    assert vool_window._has_webview2() is False
    (runtime / "msedgewebview2.exe").touch()
    assert vool_window._has_webview2() is True


def test_build_bundle_copies_every_runtime_package_and_stays_lean() -> None:
    ps1 = (_BUNDLE / "build_bundle.ps1").read_text(encoding="utf-8")
    # Every top-level package the server imports must be copied (verified by running the bundle).
    for pkg in ("apps", "core", "adapters", "storage", "network", "relay", "retrieval", "sandbox", "tools", "channels", "ops", "config"):
        assert f'"{pkg}"' in ps1, f"build_bundle.ps1 must copy the {pkg} package"
    # Lean deps only; the heavy ML stack must not be pip-installed into the bundle.
    for lean in ("pydantic", "cryptography", "uvicorn", "starlette", "solders"):
        assert lean in ps1
    # These packages are part of the served runtime import closure or its declared base
    # requirements. They must be installed into the embedded interpreter, not assumed to
    # exist on the user's machine.
    for served_runtime_dep in (
        "idna==3.18",
        "reportlab==5.0.1",
        "markdown-it-py==4.2.0",
        "zstandard>=0.22.0",
        "xxhash>=3.4.0",
    ):
        assert served_runtime_dep in ps1, f"embedded bundle must install {served_runtime_dep}"
    assert "pip install" in ps1 and "torch" not in ps1  # training-only stack never enters the bundle
    assert "Embedded Python pip bootstrap failed." in ps1
    assert "Embedded Python setuptools/wheel bootstrap failed." in ps1
    assert "Embedded Python runtime dependency installation failed." in ps1
    assert "& $py -m pip check" in ps1
    assert "Embedded Python dependency check failed." in ps1
    # Native window: pywebview/pythonnet deps + the host script copied in.
    assert "pywebview" in ps1 and "pythonnet" in ps1
    assert "vool_window.py" in ps1
    assert "bundle_supervisor.py" in ps1
    assert "bundle_manifest.json" in ps1
    assert "WebView2Installer" in ps1 and "WebView2Sha256" in ps1
    assert "Get-AuthenticodeSignature" in ps1
    assert "webview2_installer_sha256" in ps1
    assert 'Copy-Item (Join-Path $PSScriptRoot "vool-open.ps1")' not in ps1
    assert '"installer\\stamp_build_source.py"' in ps1
    assert 'Join-Path $appDir "config\\build-source.json"' in ps1
    for field in ("app_version", "source_commit", "source_dirty_state"):
        assert field in ps1
    for canonical_source in (
        "README.md",
        "docs\\SYSTEM_SPINE.md",
        "docs\\STATUS.md",
        "docs\\VOOL_DESKTOP_BLUEPRINT.md",
    ):
        assert f'"{canonical_source}"' in ps1
    assert "Canonical grounding source missing" in ps1


def test_build_bundle_copies_the_ollama_runner_runtime() -> None:
    ps1 = (_BUNDLE / "build_bundle.ps1").read_text(encoding="utf-8")
    assert '"lib\\ollama"' in ps1
    assert "llama-server.exe" in ps1
    assert "Ollama runtime support directory not found" in ps1
    assert "Ollama runner missing from the installation" in ps1


def test_iss_packages_stage_and_installs_per_user() -> None:
    iss = (_BUNDLE / "vool.iss").read_text(encoding="utf-8")
    assert "OutputBaseFilename={#InstallerBaseName}" in iss
    assert 'Source: "{#Stage}\\app\\*"' in iss
    assert 'Source: "{#Stage}\\ollama\\*"' in iss
    assert 'DestDir: "{localappdata}\\VOOL\\models"' in iss
    assert "uninsneveruninstall" in iss
    assert 'DestDir: "{app}\\models"' not in iss
    assert 'Source: "{#Stage}\\python\\*"' in iss
    assert 'Source: "{#Stage}\\native\\host\\*"' in iss
    assert 'Source: "{#Stage}\\native\\broker\\*"' in iss
    assert 'Filename: "{app}\\native\\VOOL.exe"' in iss
    assert 'Source: "{#Stage}\\bundle_manifest.json"' in iss
    assert 'Source: "{#Stage}\\VOOL.cmd"' in iss
    assert 'Source: "{#Stage}\\webview2\\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"' in iss
    assert "PrepareToInstall" in iss and "WebView2Present" in iss
    assert 'Source: "{#Stage}\\vool-open.ps1"' not in iss
    assert "PrivilegesRequired=lowest" in iss  # no admin needed
    assert "vool.vbs" in iss  # shortcuts launch windowless via the VBS wrapper (no console flash)
    assert "{#Stage}" in iss  # packages the staging dir
    # uninstall must NOT delete the user's data/models dir (wallet, keys, LLMs survive)
    assert 'Type: filesandordirs; Name: "{localappdata}\\VOOL"' not in iss
    assert "AppMutex=Local\\VOOL_BUNDLE_SUPERVISOR,Local\\VOOL_WINDOW_SINGLETON" in iss
    assert "StopNulla" not in iss and "Stop-Process" not in iss
    assert "AppPublisher={#AppPublisher}" in iss
    assert "CloseApplications=no" in iss


def test_windows_bundle_publishes_native_host_and_broker() -> None:
    ps1 = (_BUNDLE / "build_bundle.ps1").read_text(encoding="utf-8")
    assert 'ValidateSet("Internal", "Release")' in ps1
    assert "windows_authority_audit --release-check" in ps1
    assert "VOOL.WindowsHost.csproj" in ps1
    assert "Vool.WindowsBroker.csproj" in ps1
    assert 'build_mode = $BuildMode.ToLowerInvariant()' in ps1
    supervisor = (_BUNDLE / "bundle_supervisor.py").read_text(encoding="utf-8")
    assert 'self.root / "native" / "VOOL.exe"' in supervisor


def test_vbs_runs_the_launcher_hidden() -> None:
    vbs = (_BUNDLE / "vool.vbs").read_text(encoding="utf-8")
    assert "VOOL.cmd" in vbs
    assert ", 0, False" in vbs  # WShell.Run window style 0 = hidden


# --- Close-to-Dock (macOS): red X minimizes VOOL instead of quitting (Spotify-style) ---

class _FakeClosingEvent:
    """Stands in for pywebview's synchronous 'closing' event: captures the += handler."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _FakeWindow:
    def __init__(self) -> None:
        self.events = type("E", (), {"closing": _FakeClosingEvent()})()
        self.hidden = 0

    def hide(self) -> None:
        self.hidden += 1


def test_close_to_dock_is_default_on_and_macos_only(monkeypatch) -> None:
    # Default ON since c8f74d1: the red X hides the window to the Dock and the app stays live.
    # Correctness rests on hooking the WINDOW's windowShouldClose_ rather than pywebview's
    # events.closing -- the earlier hook conflated the red X with Cmd+Q and made the app unquittable,
    # which is why this used to ship off. VOOL_CLOSE_TO_DOCK=0 is the escape hatch.
    from installer.bundle import vool_window as nw

    monkeypatch.setattr(nw.sys, "platform", "darwin")
    monkeypatch.delenv("VOOL_CLOSE_TO_DOCK", raising=False)
    assert nw._close_to_dock_enabled() is True  # ON by default
    for falsey in ("0", "false", "no", "off"):
        monkeypatch.setenv("VOOL_CLOSE_TO_DOCK", falsey)
        assert nw._close_to_dock_enabled() is False, falsey
    monkeypatch.setenv("VOOL_CLOSE_TO_DOCK", "1")
    assert nw._close_to_dock_enabled() is True
    # Never on the other packaging lane -- Windows/Linux window UX is KAS's.
    for other in ("win32", "linux"):
        monkeypatch.setattr(nw.sys, "platform", other)
        assert nw._close_to_dock_enabled() is False, other


def test_dock_behavior_is_installed_after_start_when_enabled(monkeypatch) -> None:
    # The delegates must be installed on the main thread AFTER the run loop is up, so the wiring is a
    # post-start callback rather than something done at window-construction time.
    from installer.bundle import vool_window as nw

    monkeypatch.setattr(nw.sys, "platform", "darwin")
    monkeypatch.delenv("VOOL_CLOSE_TO_DOCK", raising=False)
    assert callable(nw._dock_post_start(_FakeWindow()))


def test_dock_behavior_not_installed_when_opted_out(monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    monkeypatch.setattr(nw.sys, "platform", "darwin")
    monkeypatch.setenv("VOOL_CLOSE_TO_DOCK", "0")  # escape hatch -> red X quits as before
    assert nw._dock_post_start(_FakeWindow()) is None
    monkeypatch.setattr(nw.sys, "platform", "win32")
    monkeypatch.delenv("VOOL_CLOSE_TO_DOCK", raising=False)
    assert nw._dock_post_start(_FakeWindow()) is None


def test_native_window_host_hides_on_red_x_without_vetoing_cmd_q() -> None:
    py = (_BUNDLE / "vool_window.py").read_text(encoding="utf-8")
    assert "_install_dock_behavior" in py
    assert "windowShouldClose_" in py  # the red X -- a WINDOW-delegate selector
    assert "orderOut_" in py  # hide, not close (app + Dock icon stay alive)
    assert "applicationShouldHandleReopen_" in py  # Dock-icon click reopens the window
    assert 'sys.platform != "darwin"' in py  # macOS-only; Windows/Linux lane untouched
    assert "VOOL_CLOSE_TO_DOCK" in py  # env escape hatch back to close-quits
    # The regression guard: Cmd+Q must NOT be intercepted. applicationShouldTerminate_ is the
    # APP-delegate selector the old events.closing hook effectively vetoed; it is referenced in the
    # module docstring for that reason, so assert it is never DEFINED (which is what would veto Quit).
    assert "def applicationShouldTerminate_" not in py
    # Same for the old mechanism: events.closing fires for BOTH the red X and Cmd+Q, so it must never
    # be SUBSCRIBED to (it is named in the docstring explaining exactly this, hence the += idiom).
    assert "events.closing +=" not in py


# --- Native folder picker (js_api bridge): "+ Project" opens a real folder dialog in the VOOL window ---

class _FakeDialogWindow:
    def __init__(self, result) -> None:
        self._result = result

    def create_file_dialog(self, kind):
        return self._result


def _inject_fake_webview(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("webview")
    fake.FOLDER_DIALOG = "FOLDER_DIALOG"
    monkeypatch.setitem(sys.modules, "webview", fake)


def test_pick_folder_returns_the_chosen_path(monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    _inject_fake_webview(monkeypatch)
    api = nw._WindowApi()
    api.set_window(_FakeDialogWindow(["/Users/me/code/acme"]))
    assert api.pick_folder() == {"ok": True, "path": "/Users/me/code/acme"}


def test_pick_folder_reports_cancel(monkeypatch) -> None:
    from installer.bundle import vool_window as nw

    _inject_fake_webview(monkeypatch)
    api = nw._WindowApi()
    api.set_window(_FakeDialogWindow(None))  # dialog dismissed
    assert api.pick_folder() == {"ok": False, "cancelled": True}


def test_pick_folder_without_a_window_is_soft() -> None:
    from installer.bundle import vool_window as nw

    assert nw._WindowApi().pick_folder() == {"ok": False, "error": "no_window"}


def test_native_window_exposes_the_folder_picker_bridge() -> None:
    py = (_BUNDLE / "vool_window.py").read_text(encoding="utf-8")
    assert "js_api=api" in py  # the bridge is passed to create_window
    assert "def pick_folder" in py and "FOLDER_DIALOG" in py  # native folder dialog
    assert "create_file_dialog" in py
