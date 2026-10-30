"""The bundle supervisor's children die with it on Windows, even when it is killed hard.

G4 orphan check on Steve (177621c): in the bundle layout, ``taskkill /F`` on the bundle supervisor
alone left the API (and its media worker) running and holding port 11435. Every child the bundle
supervisor starts now joins one Job object with KILL_ON_JOB_CLOSE whose only handle the supervisor
holds, so Windows ends them when the supervisor ends, however it ends. The job needs kernel32, so
it is created only on a real Windows host (``_JOB_HOST``); these tests stand in a recording fake.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor
from tests._windows_system32 import TASKKILL, trust_system32


class _Job:
    def __init__(self) -> None:
        self.events: list[str] = []

    def assign(self, pid: int) -> None:
        self.events.append(f"assign:{pid}")

    def close(self) -> None:
        self.events.append("close")


def _supervisor(tmp_path: Path, monkeypatch, job_factory) -> BundleSupervisor:
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervisor._prepare()
    supervisor.log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(bundle_supervisor, "_JOB_HOST", True)
    trust_system32(monkeypatch)
    supervisor._job_factory = job_factory
    return supervisor


def _child(pid: int) -> mock.Mock:
    child = mock.Mock(pid=pid)
    child.poll.return_value = None
    child.wait.return_value = 0
    return child


def test_every_started_child_joins_one_supervisor_job(tmp_path, monkeypatch) -> None:
    jobs: list[_Job] = []

    def factory() -> _Job:
        jobs.append(_Job())
        return jobs[-1]

    supervisor = _supervisor(tmp_path, monkeypatch, factory)
    with mock.patch.object(bundle_supervisor.subprocess, "Popen", side_effect=[_child(501), _child(502)]):
        supervisor._start("ollama", ["ollama", "serve"], cwd=tmp_path)
        supervisor._start("api", ["python", "api.py"], cwd=tmp_path)

    assert len(jobs) == 1
    assert jobs[0].events == ["assign:501", "assign:502"]
    assert set(supervisor.processes) == {"ollama", "api"}


def test_without_a_job_no_child_is_started_and_a_retry_is_scheduled(tmp_path, monkeypatch) -> None:
    def no_job():
        raise OSError(5, "Access is denied")

    supervisor = _supervisor(tmp_path, monkeypatch, no_job)
    with mock.patch.object(bundle_supervisor.subprocess, "Popen") as popen:
        supervisor._start("api", ["python", "api.py"], cwd=tmp_path)

    popen.assert_not_called()
    assert "api" not in supervisor.processes
    assert supervisor.failures["api"] == 1
    assert "job" in supervisor.last_exit["api"]["reason"]


def test_a_child_that_cannot_join_the_job_is_stopped_and_retried(tmp_path, monkeypatch) -> None:
    class _RefusingJob(_Job):
        def assign(self, pid: int) -> None:
            raise OSError(5, "Access is denied")

    supervisor = _supervisor(tmp_path, monkeypatch, _RefusingJob)
    child = _child(503)
    with (
        mock.patch.object(bundle_supervisor.subprocess, "Popen", return_value=child),
        mock.patch.object(bundle_supervisor.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
    ):
        supervisor._start("api", ["python", "api.py"], cwd=tmp_path)

    assert "api" not in supervisor.processes
    assert run.call_args.args[0] == [TASKKILL, "/PID", "503", "/T", "/F"]
    assert supervisor.failures["api"] == 1


def test_the_job_is_closed_after_the_children_are_stopped(tmp_path, monkeypatch) -> None:
    job = _Job()
    supervisor = _supervisor(tmp_path, monkeypatch, lambda: job)
    stopped: list[str] = []
    monkeypatch.setattr(supervisor, "_acquire_lease", lambda: True)
    monkeypatch.setattr(supervisor, "_release_lease", lambda: None)
    for step in ("_reap_dead", "_ensure_ollama", "_reap_window", "_consume_window_reopen", "_ensure_window"):
        monkeypatch.setattr(supervisor, step, lambda *args: None)

    def ensure_api() -> None:
        with mock.patch.object(bundle_supervisor.subprocess, "Popen", return_value=_child(504)):
            supervisor._start("api", ["python", "api.py"], cwd=tmp_path)

    monkeypatch.setattr(supervisor, "_ensure_api", ensure_api)
    monkeypatch.setattr(
        supervisor, "_stop_child", lambda name: stopped.append(name) or job.events.append(f"stop:{name}")
    )

    supervisor.run(once=True)

    assert job.events[0] == "assign:504"
    assert job.events[-1] == "close"
    assert job.events.index("stop:api") < job.events.index("close")
