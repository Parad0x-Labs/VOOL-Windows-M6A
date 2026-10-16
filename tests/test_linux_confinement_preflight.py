from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from ops import check_linux_confinement as preflight
from sandbox.job_runner import JobRunner
from sandbox.resource_limits import ExecutionPolicy


@pytest.fixture
def linux_user(monkeypatch):
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 1001, raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/bwrap")


@pytest.mark.parametrize("returncodes", ((0, 0), (1, 0), (0, 1), (1, 1), (-11, 0)))
def test_both_exact_probes_run_once_and_preserve_results(linux_user, monkeypatch, returncodes):
    run = Mock(side_effect=[subprocess.CompletedProcess([], rc, "output", "raw denial") for rc in returncodes])
    monkeypatch.setattr(preflight.subprocess, "run", run)
    report = preflight.collect_report()
    assert report["ok"] == (returncodes == (0, 0))
    assert run.call_count == 2
    for i, denied in enumerate((True, False)):
        expected = ["/usr/bin/bwrap", *(["--unshare-net"] if denied else []),
                    "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--", "/bin/sh", "-c", "exec 3<>/dev/null"]
        assert report["probes"][i] == {
            "deny_network": denied, "argv": expected, "returncode": returncodes[i],
            "stdout": "output", "stderr": "raw denial", "ok": returncodes[i] == 0,
        }
        assert run.call_args_list[i].args == (expected,)
        assert run.call_args_list[i].kwargs == {
            "capture_output": True, "text": True, "shell": False, "check": False, "timeout": 10,
        }


@pytest.mark.parametrize("denied", (True, False))
def test_preflight_commands_match_the_actual_runtime_probe(linux_user, monkeypatch, tmp_path, denied):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(preflight.subprocess, "run", run)
    report = preflight.collect_report()
    captured = []
    monkeypatch.setattr("sandbox.job_runner._backend_usable", lambda name, argv: captured.append(argv) or True)
    runner = JobRunner(ExecutionPolicy(workspace_root=tmp_path))
    # os.name remains native: this is a prefix contract, not an actual Linux namespace test.
    monkeypatch.setattr("sandbox.job_runner.os.name", "posix")
    runner._linux_bwrap_prefix(["true"], (), deny_network=denied)
    assert captured == [next(p["argv"] for p in report["probes"] if p["deny_network"] == denied)]


@pytest.mark.parametrize("fault", ("missing", "root", "wrong_platform"))
def test_ineligible_context_never_starts_a_probe(linux_user, monkeypatch, fault):
    if fault == "missing":
        monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    elif fault == "root":
        monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    else:
        monkeypatch.setattr(preflight.sys, "platform", "win32")
    run = Mock()
    monkeypatch.setattr(preflight.subprocess, "run", run)
    report = preflight.collect_report()
    assert not report["ok"] and report["error"] and report["probes"] == []
    run.assert_not_called()


@pytest.mark.parametrize("fault", (
    subprocess.TimeoutExpired(["bwrap"], 10, output=b"partial", stderr=b"denied"),
    PermissionError(13, "permission denied"),
    FileNotFoundError(2, "missing binary"),
))
def test_probe_fault_is_retained_without_retry(linux_user, monkeypatch, fault):
    run = Mock(side_effect=[fault, subprocess.CompletedProcess([], 0, "", "")])
    monkeypatch.setattr(preflight.subprocess, "run", run)
    report = preflight.collect_report()
    assert not report["ok"] and run.call_count == 2
    first, second = report["probes"]
    assert first["returncode"] is None and first["error"]
    assert second["ok"]
    if isinstance(fault, subprocess.TimeoutExpired):
        assert first["stdout"] == "partial" and first["stderr"] == "denied"
    else:
        assert first["errno"] == fault.errno


@pytest.mark.parametrize("ok", (True, False))
def test_cli_saves_and_prints_report_before_returning_status(monkeypatch, tmp_path, capsys, ok):
    report = {"schema": "fixture", "ok": ok, "probes": []}
    monkeypatch.setattr(preflight, "collect_report", lambda: report)
    path = tmp_path / "logs" / "confinement.json"
    assert preflight.main(["--report", str(path)]) == (0 if ok else 1)
    assert json.loads(path.read_text()) == json.loads(capsys.readouterr().out) == report


def test_evidence_write_failure_cannot_publish_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(preflight, "collect_report", lambda: {"ok": True})
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=PermissionError("evidence denied")))
    assert preflight.main(["--report", str(tmp_path / "report.json")]) == 1
    captured = capsys.readouterr()
    assert "evidence denied" in captured.err and json.loads(captured.out)["ok"]


@pytest.mark.parametrize("job_name,log_root", (
    ("verify-shard", ".verification-logs"), ("compatibility", ".compatibility-logs"),
))
@pytest.mark.parametrize("fault", ("none", "update", "install", "version"))
def test_actual_setup_shell_preserves_failure_exit_and_log(tmp_path, job_name, log_root, fault):
    bash = (str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe")
            if os.name == "nt" else shutil.which("bash"))
    assert bash and Path(bash).is_file(), "The setup contract requires a real bash executable."
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    setup = next(step["run"] for step in workflow["jobs"][job_name]["steps"]
                 if step.get("name") == "Provision Linux confinement (Bubblewrap)")
    # Replace only fixture commands; the real workflow shell body executes unchanged.
    fixtures = f"""
    sudo() {{
      printf '%s\\n' "fixture:$*"
      if [ '{fault}' = update ] && [ "$2" = update ]; then return 7; fi
      if [ '{fault}' = install ] && [ "$2" = install ]; then return 7; fi
      return 0
    }}
    dpkg-query() {{ printf '%s\\n' 'fixture:package-version'; }}
    bwrap() {{ printf '%s\\n' 'fixture:bwrap-version'; [ '{fault}' != version ]; }}
    """
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    completed = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", fixtures + setup],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == (0 if fault == "none" else 1 if fault == "version" else 7)
    log = (tmp_path / log_root / "confinement-provision.log").read_text()
    assert "fixture:apt-get update" in log and log in completed.stdout
    assert ("fixture:apt-get install" in log) == (fault != "update")
    assert ("fixture:bwrap-version" in log) == (fault in {"none", "version"})
