"""The Windows bundle supervisor backs off a child that exits during a pass, like any other exit.

Each pass first reaps a child that has exited (_reap_dead: the exit is logged as the child's last
exit, counted, and its next start waits out the restart backoff), then checks each child's health.
A child that exited after that reap and before its own check -- while Ollama was checked or stopped,
or during the API's own 1.5 s check -- failed the check with no live process behind it, and the
check started a new one in the same pass: no exit recorded, no failure counted and no backoff, so a
child that crashes soon after each start was started again on every such pass.

These run the supervisor off Windows (no taskkill, no PowerShell scan) on a clock that moves only
when a test moves it. Every child is a mock; no process is started.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle.bundle_supervisor import BundleSupervisor


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_Rig = tuple[BundleSupervisor, _Clock, list[mock.Mock]]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    clock = _Clock()
    monkeypatch.setattr(
        bundle_supervisor,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, time=time.time, strftime=time.strftime, sleep=lambda _s: None),
    )
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: False)
    root = tmp_path / "bundle"
    root.mkdir()
    supervisor = BundleSupervisor(root, env={"VOOL_HOME": str(tmp_path / "home")})
    supervisor.run_dir.mkdir(parents=True, exist_ok=True)
    supervisor.log_dir.mkdir(parents=True, exist_ok=True)
    children: list[mock.Mock] = []

    def popen(argv: list[str], **kwargs: object) -> mock.Mock:
        child = mock.Mock(pid=1001 + 2 * len(children))  # odd: no Windows process has this pid
        child.poll.return_value = None
        child.wait.return_value = 0
        children.append(child)
        return child

    monkeypatch.setattr(bundle_supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(supervisor, "_healthy", lambda _url: False)  # nothing answers in these tests
    yield supervisor, clock, children
    for handle in supervisor.handles.values():  # the children's log files
        with contextlib.suppress(OSError):
            handle.close()


@pytest.mark.parametrize("name", ["ollama", "api"])
def test_a_child_that_exits_after_the_reap_is_backed_off_not_started_again_at_once(rig: _Rig, name: str) -> None:
    supervisor, clock, children = rig
    ensure = supervisor._ensure_ollama if name == "ollama" else supervisor._ensure_api

    ensure()  # nothing answers yet, so the child is started
    [child] = children
    log = supervisor.handles[name]

    clock.advance(2.0)
    supervisor._reap_dead(name)  # the next pass's reap finds it running...
    child.poll.return_value = 1  # ...and it exits before its own health check
    ensure()

    # The same as an exit the reap had seen: recorded, counted, and no start in this pass.
    assert len(children) == 1
    assert name not in supervisor.processes
    assert name not in supervisor.handles and log.closed
    assert supervisor.failures[name] == 1
    assert supervisor.last_exit[name]["reason"] == "exited returncode=1"
    assert supervisor.last_exit[name]["retry_in"] == 2

    # Its next start waits for the retry, then happens.
    clock.advance(1.0)
    supervisor._reap_dead(name)
    ensure()
    assert len(children) == 1
    clock.advance(1.5)
    supervisor._reap_dead(name)
    ensure()
    assert len(children) == 2
    assert supervisor.processes[name] is children[1]


@pytest.mark.parametrize("name", ["ollama", "api"])
def test_a_child_that_exits_before_the_reap_is_backed_off_as_before(rig: _Rig, name: str) -> None:
    # Guard: the reap's own path, which already backed off an exit it saw.
    supervisor, clock, children = rig
    ensure = supervisor._ensure_ollama if name == "ollama" else supervisor._ensure_api

    ensure()
    [child] = children
    child.poll.return_value = 1
    clock.advance(2.0)
    supervisor._reap_dead(name)
    ensure()

    assert len(children) == 1
    assert supervisor.failures[name] == 1
    assert supervisor.last_exit[name]["reason"] == "exited returncode=1"
