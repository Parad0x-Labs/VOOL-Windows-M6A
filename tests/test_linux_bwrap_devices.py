"""Device access is part of confinement usability, not an unconfined fallback."""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

import pytest

from ops import check_linux_confinement as preflight
from sandbox import job_runner
from sandbox.job_runner import JobRunner
from sandbox.resource_limits import ExecutionPolicy


@pytest.mark.parametrize("denied", (True, False))
def test_private_devices_are_mounted_after_readonly_host_before_workspace(tmp_path, denied):
    runner = JobRunner(ExecutionPolicy(workspace_root=tmp_path))
    workspace = str(tmp_path.resolve())
    command = ["python3", "-c", "print('literal; not a shell command')"]
    probe = Mock(return_value=True)
    with patch.object(job_runner.os, "name", "posix"), patch.object(job_runner.sys, "platform", "linux"), patch.object(
        job_runner.shutil, "which", return_value="/usr/bin/bwrap"
    ), patch.object(job_runner, "_backend_usable", probe):
        wrapped = runner._linux_bwrap_prefix(command, (tmp_path,), deny_network=denied)

    base = ["/usr/bin/bwrap", *(["--unshare-net"] if denied else []),
            "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp"]
    assert wrapped == [*base, "--bind", workspace, workspace, "--", *command]
    probe.assert_called_once_with(
        f"bwrap:deny_network={denied}", [*base, "--", "/bin/sh", "-c", "exec 3<>/dev/null"]
    )
    assert command == ["python3", "-c", "print('literal; not a shell command')"]
    assert wrapped.count("--dev") == 1
    assert wrapped[wrapped.index("--bind") + 1:wrapped.index("--bind") + 3] == [workspace, workspace]


@pytest.mark.parametrize("denied", (True, False))
def test_a_device_open_failure_makes_the_bwrap_backend_unusable(tmp_path, denied):
    runner = JobRunner(ExecutionPolicy(workspace_root=tmp_path))
    run = Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"device open denied"))
    with patch.object(job_runner.os, "name", "posix"), patch.object(job_runner.sys, "platform", "linux"), patch.object(
        job_runner.shutil, "which", return_value="/usr/bin/bwrap"
    ), patch.object(job_runner.subprocess, "run", run):
        for _ in range(2):
            with pytest.raises(job_runner.KernelIsolationUnavailableError, match="private devices"):
                runner._linux_bwrap_prefix(["python3", "-V"], (), deny_network=denied)

    assert job_runner._BACKEND_USABLE[f"bwrap:deny_network={denied}"] is False
    assert run.call_count == 1
    assert run.call_args.args[0][-4:] == ["--", "/bin/sh", "-c", "exec 3<>/dev/null"]
    assert run.call_args.kwargs == {"capture_output": True, "timeout": 10, "shell": False, "check": False}


def test_preflight_proves_device_access_in_both_network_modes(monkeypatch):
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 1001, raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/bwrap")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(preflight.subprocess, "run", run)

    report = preflight.collect_report()

    assert report["ok"] is True and run.call_count == 2
    for denied, call in zip((True, False), run.call_args_list, strict=True):
        expected = ["/usr/bin/bwrap", *(["--unshare-net"] if denied else []),
                    "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--", "/bin/sh", "-c", "exec 3<>/dev/null"]
        assert call.args == (expected,)
        assert next(probe for probe in report["probes"] if probe["deny_network"] == denied)["argv"] == expected


@pytest.mark.parametrize("denied", (True, False))
def test_a_failed_device_probe_cannot_fall_back_to_network_only_isolation(tmp_path, denied):
    runner = JobRunner(ExecutionPolicy(workspace_root=tmp_path, allow_network_egress=not denied))
    fallback = Mock(return_value=["network-only-backend", "--", "python3", "-V"])
    with patch.object(job_runner.os, "name", "posix"), patch.object(job_runner.sys, "platform", "linux"), patch.object(
        job_runner.shutil, "which", return_value="/usr/bin/bwrap"
    ), patch.object(job_runner, "_backend_usable", return_value=False), patch.object(
        runner, "_linux_unshare_prefix", fallback
    ), patch.object(runner, "_linux_firejail_prefix", fallback):
        with pytest.raises(job_runner.KernelIsolationUnavailableError):
            runner._with_network_isolation(["python3", "-V"], (tmp_path,))
    fallback.assert_not_called()


@pytest.mark.parametrize("root", ("/", "/dev", "/dev/pts", "/dev/null"))
@pytest.mark.parametrize("denied", (True, False))
def test_writable_roots_cannot_replace_private_devices(tmp_path, root, denied):
    runner = JobRunner(ExecutionPolicy(workspace_root=tmp_path))
    # Model a resolved Linux path without changing this host's pathlib implementation.
    writable = Mock(spec=Path)
    writable.resolve.return_value = PurePosixPath(root)
    probe = Mock(return_value=True)
    with patch.object(job_runner.os, "name", "posix"), patch.object(job_runner.sys, "platform", "linux"), patch.object(
        job_runner.shutil, "which", return_value="/usr/bin/bwrap"
    ), patch.object(job_runner, "_backend_usable", probe):
        with pytest.raises(job_runner.KernelIsolationUnavailableError):
            runner._linux_bwrap_prefix(["python3", "-V"], (writable,), deny_network=denied)
    probe.assert_not_called()
