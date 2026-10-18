"""The Windows bundle's window host attaches to the API the bundle supervisor owns.

The supervisor starts the API, then the window. The window host defaults to owning its runtime:
it refuses an occupied API port, and with no expected commit (a bundle has no .git to ask) it
refuses every runtime. Installed, the window exited at once and never opened (install smoke run
39094759487). The supervisor now starts it attached, expecting exactly the commit
build_bundle.ps1 stamped into the bundle.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from installer.bundle import native_runtime_supervisor as nrs
from installer.bundle.bundle_supervisor import BundleSupervisor

_COMMIT = "1771314cff63" + "0" * 28


def _bundle(tmp_path: Path, *, stamped: bool = True) -> BundleSupervisor:
    root = tmp_path / "bundle"
    (root / "app" / "config").mkdir(parents=True)
    if stamped:
        (root / "app" / "config" / "build-source.json").write_text(
            json.dumps({"source_kind": "git", "commit_full": _COMMIT, "dirty_state": False}), encoding="utf-8"
        )
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervisor._prepare()
    return supervisor


def _started(supervisor: BundleSupervisor, start, *, api_healthy: bool = True) -> dict:
    # The window opens only once the API answers; the API is started only while it does not.
    child = mock.Mock(pid=303)
    child.poll.return_value = None
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", return_value=child) as popen, \
            mock.patch.object(supervisor, "_healthy", return_value=api_healthy):
        start()
    assert popen.call_count == 1
    return popen.call_args


def test_the_window_starts_attached_and_expects_the_stamped_commit(tmp_path) -> None:
    supervisor = _bundle(tmp_path)
    call = _started(supervisor, supervisor._ensure_window)
    argv, env = call.args[0], call.kwargs["env"]
    assert argv[-1].endswith("vool_window.py")
    assert env["VOOL_NATIVE_REQUIRE_OWNED_RUNTIME"] == "0"
    assert env["VOOL_EXPECTED_COMMIT"] == _COMMIT
    supervisor._stop_child("window")


def test_the_attach_override_is_the_windows_alone(tmp_path) -> None:
    supervisor = _bundle(tmp_path)
    with mock.patch.object(supervisor, "_clear_stale_api_processes"):
        call = _started(supervisor, supervisor._ensure_api, api_healthy=False)
    env = call.kwargs["env"]
    assert "VOOL_NATIVE_REQUIRE_OWNED_RUNTIME" not in env
    assert "VOOL_EXPECTED_COMMIT" not in env
    supervisor._stop_child("api")


def test_an_unstamped_bundle_gives_the_window_no_commit_to_trust(tmp_path) -> None:
    supervisor = _bundle(tmp_path, stamped=False)
    call = _started(supervisor, supervisor._ensure_window)
    assert call.kwargs["env"]["VOOL_EXPECTED_COMMIT"] == ""
    supervisor._stop_child("window")


def test_the_window_host_reads_that_environment_as_attach_to_the_stamped_commit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOOL_NATIVE_REQUIRE_OWNED_RUNTIME", "0")
    monkeypatch.setenv("VOOL_EXPECTED_COMMIT", _COMMIT)
    supervisor = nrs.NativeRuntimeSupervisor.from_environment(module_path=tmp_path / "bundle" / "vool_window.py")
    assert supervisor.require_owned is False
    assert supervisor.expected_sha == _COMMIT
