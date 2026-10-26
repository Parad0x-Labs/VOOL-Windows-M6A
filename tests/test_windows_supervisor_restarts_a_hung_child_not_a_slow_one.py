"""The Windows bundle supervisor restarts a hung child, never one that answered one check late.

Installed, bundle_supervisor.py stopped the API -- or Ollama, with its runner and any model pull --
on the FIRST health check that missed its 1.5 s timeout once the child was a minute old. One busy
/healthz lost the in-flight reply, and the window, vouched for by the API's pid, exited 3 and
reopened. Around that rule:

- ages and retry times ran on the wall clock, so a clock change or a sleep counted as age, and a
  clock set back held a retry back for as long as it was set back;
- the restart backoff never forgot old failures, so it only grew over the app's life;
- the probe went through any configured HTTP proxy and followed redirects, so something other than
  the child could answer for 127.0.0.1 (now only the child's own 2xx answer is healthy; a redirect
  or any other status is unhealthy);
- an answer that is not HTTP (BadStatusLine) escaped run(), whose finally stopped every child with
  nothing left to restart them;
- the stale-API PowerShell scan ran on every 2 s pass while the API backed off, with no timeout,
  and asked for its output decoded as text, which it never reads;
- a window the supervisor reopened after its runtime was lost, whose host's one readiness check of
  the API missed so that it exited 1 without showing a window, was taken for the user's close: no
  window came back while that API ran.

A child that has answered is now restarted only after three unanswered checks in a row spanning
40 s (the API) or 60 s (Ollama). That leaves a stall of the API to the window host's own 30 s grace:
the window rides out a stall of up to 30 s, closes (exit 3) on a longer one and reopens on the same
API once it answers, and reopens on a new API when the supervisor restarts one that stays hung: 40 s
plus up to two passes after its first unanswered check (about 45 s; 48 s with Ollama hung too; up to
about 56 s when an Ollama stop with an 8 s taskkill falls inside the API's 40 s).

These run the supervisor on a fake machine whose wall clock and tick count (Windows' monotonic
clock, GetTickCount64, which counts sleep) move only when a test moves them. No real Ollama, API,
PowerShell or taskkill runs: the platform seam is pinned off Windows unless a test replaces this
module's subprocess, every child is a mock whose pid no Windows process can have (Windows pids are
multiples of 4), and the only sockets are stubs on 127.0.0.1 ephemeral ports.
"""
from __future__ import annotations

import contextlib
import heapq
import http.client
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from installer.bundle import bundle_supervisor
from installer.bundle import native_runtime_supervisor as nrs
from installer.bundle.bundle_supervisor import BundleSupervisor

# The repository, where a fresh interpreter imports the supervisor from.
_ROOT = Path(__file__).resolve().parents[1]
# The supervisor waits this long between passes.
_POLL = 2.0
# A health probe that gets no answer waits this long, in the supervisor and in the window host.
_PROBE = 1.5
# One supervisor pass while a child does not answer: the 2 s poll plus a probe that times out.
_PASS = _POLL + _PROBE
# The window host's watchdog waits this long after one check ends before the next starts
# (VOOL_RUNTIME_WATCHDOG_POLL in vool_window.py); each check is its own 1.5 s probe.
_WINDOW_POLL = 2.0
# A restarted API answers its first /healthz this long after it was started.
_API_BOOT = 8.0
_COMMIT = "4899671" + "0" * 33
_ENSURE = {"api": "_ensure_api", "ollama": "_ensure_ollama"}
_PROGRAM = {"api": "vool_api_server.py", "ollama": "ollama.exe", "window": "vool_window.py"}


class _Machine:
    """The clocks the supervisor reads. ``awake`` and ``asleep`` move both (Windows' tick count goes
    on counting while the machine sleeps); ``set_clock`` moves only the wall clock."""

    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.tick = 86_400.0
        self.sleeps = 0
        self.after_sleep: Callable[[int], None] | None = None

    def awake(self, seconds: float) -> None:
        self.wall += seconds
        self.tick += seconds

    asleep = awake

    def at(self, tick: float) -> None:
        assert tick >= self.tick
        self.awake(tick - self.tick)

    def set_clock(self, seconds: float) -> None:
        self.wall += seconds

    def _sleep(self, seconds: float) -> None:
        self.awake(seconds)
        self.sleeps += 1
        if self.after_sleep is not None:
            self.after_sleep(self.sleeps)

    def time_module(self) -> SimpleNamespace:
        return SimpleNamespace(time=lambda: self.wall, monotonic=lambda: self.tick, sleep=self._sleep, strftime=time.strftime)


class _Children:
    """Stands in for subprocess.Popen: each start is a live mock child. It records each child's
    environment and the tick it was started and stopped (terminated) at."""

    def __init__(self, machine: _Machine) -> None:
        self.machine = machine
        self.started: list[tuple[list[str], mock.Mock, dict[str, str]]] = []
        self.started_tick: dict[int, float] = {}
        self.stopped_tick: dict[int, float] = {}

    def __call__(self, argv: list[str], **kwargs: object) -> mock.Mock:
        child = mock.Mock(pid=4001 + 2 * len(self.started))
        child.poll.return_value = None
        child.wait.return_value = 0
        child.terminate.side_effect = lambda: self.stopped_tick.setdefault(child.pid, self.machine.tick)
        env = kwargs.get("env")
        self.started.append((list(argv), child, dict(env) if isinstance(env, dict) else {}))
        self.started_tick[child.pid] = self.machine.tick
        return child

    def of(self, name: str) -> list[tuple[mock.Mock, dict[str, str]]]:
        return [(child, env) for argv, child, env in self.started if any(str(part).endswith(_PROGRAM[name]) for part in argv)]

    def count(self, name: str) -> int:
        return len(self.of(name))

    def last(self, name: str) -> mock.Mock:
        return self.of(name)[-1][0]


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> _Machine:
    fake = _Machine()
    # Only this module's reference to `time`: the global clocks pytest and other threads read stay real.
    monkeypatch.setattr(bundle_supervisor, "time", fake.time_module())
    return fake


@pytest.fixture
def supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: _Machine) -> Iterator[BundleSupervisor]:
    # The narrow platform seam: no taskkill and no PowerShell scan, on Windows too.
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: False)
    root = tmp_path / "bundle"
    root.mkdir()
    home = tmp_path / "home"
    supervising = BundleSupervisor(root, env={"VOOL_HOME": str(home), "LOCALAPPDATA": str(home)})
    supervising._prepare()
    yield supervising
    for handle in list(supervising.handles.values()):  # Windows cannot delete a child log still open
        with contextlib.suppress(OSError):
            handle.close()


@pytest.fixture
def popen(machine: _Machine) -> _Children:
    return _Children(machine)


@pytest.fixture
def stops(supervisor: BundleSupervisor, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    stopped: list[str] = []
    real = supervisor._stop_child

    def stop(name: str) -> None:
        stopped.append(name)
        real(name)

    monkeypatch.setattr(supervisor, "_stop_child", stop)
    return stopped


def _check(supervisor: BundleSupervisor, name: str, answers: bool | Callable[[str], bool], popen: _Children) -> None:
    """One pass of the supervisor loop's step for `name`, with its health check answering or not."""
    healthy = {"side_effect": answers} if callable(answers) else {"return_value": answers}
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(supervisor, "_healthy", **healthy):
        getattr(supervisor, _ENSURE[name])()


def _passes(supervisor: BundleSupervisor, machine: _Machine, name: str, answers: bool, popen: _Children, count: int) -> None:
    for _ in range(count):
        machine.awake(_PASS)
        _check(supervisor, name, answers, popen)


def _window_check(supervisor: BundleSupervisor, answers: bool, popen: _Children) -> None:
    """The supervisor loop's window steps: reap a window that exited, then the window's own check of
    the API, which opens the window when the API answers."""
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(supervisor, "_healthy", return_value=answers):
        supervisor._reap_window()
        supervisor._ensure_window()


def _outcome(call: Callable[[], object]) -> object:
    """What `call` returns, or the exception it raises: an escape fails at the assertion on it."""
    try:
        return call()
    except Exception as exc:
        return exc


def _log(supervisor: BundleSupervisor) -> str:
    return (supervisor.log_dir / "bundle-supervisor.log").read_text(encoding="utf-8")


def _run_without_the_lease(supervisor: BundleSupervisor, monkeypatch: pytest.MonkeyPatch) -> None:
    # The lease uses msvcrt on Windows, which the platform seam pinned off; it is not under test.
    monkeypatch.setattr(supervisor, "_acquire_lease", lambda: True)
    monkeypatch.setattr(supervisor, "_release_lease", lambda: None)


def _run(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: Callable[..., mock.Mock],
    healthy: Callable[[str], bool],
    after_sleep: Callable[[int], None],
) -> None:
    """The real run() loop; `after_sleep(n)` runs once its n-th 2 s wait is over."""
    _run_without_the_lease(supervisor, monkeypatch)
    machine.after_sleep = after_sleep
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
            mock.patch.object(supervisor, "_healthy", side_effect=healthy):
        assert supervisor.run() == 0


def _stop_after(supervisor: BundleSupervisor, n: int, sleeps: int) -> None:
    if n == sleeps:
        supervisor.stop_path.write_text("stop\n", encoding="utf-8")


def _windows_with(monkeypatch: pytest.MonkeyPatch, run: Callable[..., object], popen: Callable[..., object]) -> None:
    """Windows for the stale-API scan and taskkill, with this module's subprocess replaced (no global patch)."""
    monkeypatch.setattr(bundle_supervisor, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        bundle_supervisor,
        "subprocess",
        SimpleNamespace(
            run=run,
            Popen=popen,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )


# A child that has answered is restarted when it hangs, never for a late answer; its age, its
# unanswered time and its retry wait run on the monotonic clock, without time the machine slept.


def _answering_for_a_while(supervisor: BundleSupervisor, machine: _Machine, name: str, popen: _Children) -> None:
    """Start the child and let it answer for 70 s, so it is past the startup minute."""
    _check(supervisor, name, False, popen)
    assert popen.count(name) == 1
    _passes(supervisor, machine, name, True, popen, 20)


@pytest.mark.parametrize("name", ["api", "ollama"])
def test_late_answers_between_good_ones_never_restart_a_child_that_has_answered(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str], name: str
) -> None:
    _answering_for_a_while(supervisor, machine, name, popen)
    _passes(supervisor, machine, name, False, popen, 1)  # one busy /healthz or /api/tags
    assert stops == []
    for _ in range(2):
        _passes(supervisor, machine, name, False, popen, 3)  # 7 s of late answers...
        _passes(supervisor, machine, name, True, popen, 1)  # ...then a good one starts the count over
    _passes(supervisor, machine, name, False, popen, 4)
    assert stops == []
    assert popen.count(name) == 1
    assert "startup timeout" not in _log(supervisor)


def test_a_hung_api_is_restarted_after_three_misses_over_forty_seconds(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _check(supervisor, "api", False, popen)
    _passes(supervisor, machine, "api", True, popen, 1)  # it answers once, then hangs
    misses, first_miss = 0, None
    while not stops and misses < 40:
        machine.awake(_PASS)
        first_miss = machine.tick if first_miss is None else first_miss
        _check(supervisor, "api", False, popen)
        misses += 1
    assert stops == ["api"]
    # Misses at 0, 3.5, ..., 42 s: the first that is both the third or later and 40 s or more in.
    assert misses == 13
    assert machine.tick - first_miss == 42.0
    reason = supervisor.last_exit["api"]["reason"]
    assert reason.startswith("unresponsive") and "startup timeout" not in reason
    assert supervisor.failures["api"] == 1
    _passes(supervisor, machine, "api", False, popen, 1)  # the 2 s backoff is over: a new API
    assert popen.count("api") == 2
    _passes(supervisor, machine, "api", False, popen, 2)  # the new one has its startup minute
    assert stops == ["api"]


def test_answers_from_an_api_that_is_not_ours_do_not_cost_ours_its_startup_minute(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    # A stale API (an earlier run's, which the Windows-only scan did not stop) answers on the port,
    # so none of ours is started. Then it goes away, and ours starts.
    _passes(supervisor, machine, "api", True, popen, 20)
    assert popen.count("api") == 0
    _passes(supervisor, machine, "api", False, popen, 1)
    assert popen.count("api") == 1
    passes = 0
    while not stops and passes < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        passes += 1
    assert stops == ["api"]
    assert supervisor.last_exit["api"]["reason"] == "startup timeout"
    assert passes == 18  # its own startup minute: it never answered, and is stopped once 63 s old
    # 4899671 fails this only at misses == 13 below: it has no answering state, so the minute above holds there.
    # Only an answer while ours runs gives ours the rule of a child that has answered.
    _until_started(supervisor, machine, "api", popen)
    _passes(supervisor, machine, "api", True, popen, 1)
    misses = 0
    while len(stops) == 1 and misses < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        misses += 1
    assert misses == 13
    assert supervisor.last_exit["api"]["reason"].startswith("unresponsive")


@pytest.mark.parametrize(("stall", "misses"), [(25.0, 7), (28.0, 8)])
def test_a_stall_of_under_half_a_minute_after_answering_never_restarts_the_api(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str], stall: float, misses: int
) -> None:
    """Every /healthz the API gets for `stall` seconds is late (a first-run index build holding the
    GIL, paging after a resume on a slow disk), then it answers again."""
    _answering_for_a_while(supervisor, machine, "api", popen)
    stalled_until = machine.tick + stall
    late = []

    def health(_url: str) -> bool:
        if machine.tick < stalled_until:
            machine.awake(_PROBE)  # the probe waits out its timeout
            late.append(machine.tick)
            return False
        return True

    answers = 0
    while answers < 3:
        machine.awake(_POLL)
        before = len(late)
        _check(supervisor, "api", health, popen)
        answers += len(late) == before
    assert len(late) == misses
    assert late[-1] - late[0] > 20.0  # more than the 20 s a shorter span would have restarted it at
    assert stops == []
    assert popen.count("api") == 1
    assert "unresponsive" not in _log(supervisor)


def _payload(window: nrs.NativeRuntimeSupervisor, pid: int) -> dict[str, object]:
    """The /healthz answer of a VOOL API with this pid, serving the commit and home the window expects."""
    return {
        "ok": True,
        "runtime": {
            "pid": pid,
            "protocol_version": 1,
            "workstation_version": "vool-workstation-1",
            "release_version": "test",
            "commit_full": window.expected_sha,
            "runtime_home_id": window.runtime_home_id,
        },
    }


def _installed_watchdog(
    env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Callable[[], float]
) -> nrs.NativeRuntimeSupervisor:
    """The window host's watchdog as the installed window builds it -- from_environment, in the
    environment the supervisor started the window with, so with the grace, the expected commit and
    the vouching API pid that environment gives it -- on a clock the test moves."""
    for key in [key for key in os.environ if key.startswith("VOOL_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("VOOL_"):
            monkeypatch.setenv(key, value)
    installed = nrs.NativeRuntimeSupervisor.from_environment(module_path=tmp_path / "bundle" / "vool_window.py")
    return nrs.NativeRuntimeSupervisor(
        installed.project_root,
        installed.command,
        expected_sha=installed.expected_sha,
        require_owned=installed.require_owned,
        unhealthy_grace=installed.unhealthy_grace,
        vouching_pid=installed.vouching_pid,
        monotonic=clock,
    )


@pytest.mark.parametrize("lag", [0.0, 1.75, 3.4])
@pytest.mark.parametrize(
    ("stall", "outcome"),
    [(28.0, "kept"), (36.0, "reopened on the same API"), (None, "reopened on a new API")],
    ids=["stall-28s", "stall-36s", "hung"],
)
def test_the_window_watchdog_decides_a_stall_of_the_api(
    supervisor: BundleSupervisor,
    machine: _Machine,
    popen: _Children,
    stops: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stall: float | None,
    outcome: str,
    lag: float,
) -> None:
    """The window host's watchdog (the real NativeRuntimeSupervisor.assert_alive) tolerates failed
    checks of its API for its grace while the pid vouching for it lives, and closes the window (exit
    3) at once when that pid exits. With the API's 40 s span the window decides a stall: up to 30 s
    it keeps the API and the window; longer, the window closes on its own grace and reopens on the
    same API once it answers; an API that stays hung is restarted 40 s plus up to two passes after
    its first unanswered check (about 45 s here, where nothing else is stopped), and the window
    reopens on the new one.

    The API answers, then every /healthz started in the next `stall` seconds (all of them when hung)
    is late. The window's first late check starts with the stall; the supervisor's first starts
    `lag` later. Each loop checks, and after each check waits 2 s."""
    config = supervisor.root / "app" / "config"
    config.mkdir(parents=True)
    (config / "build-source.json").write_text(json.dumps({"commit_full": _COMMIT}), encoding="utf-8")
    _answering_for_a_while(supervisor, machine, "api", popen)
    api = popen.last("api")
    hung = math.inf

    def answering(at: float) -> int | None:
        """The pid of the API that answers a /healthz sent at `at`, if one does."""
        current = supervisor.processes.get("api")
        if current is None or popen.stopped_tick.get(current.pid, math.inf) <= at:
            return None
        if current is api:
            late = hung <= at and (stall is None or at < hung + stall)
            return None if late else api.pid
        return current.pid if at >= popen.started_tick[current.pid] + _API_BOOT else None

    def probe(_url: str) -> bool:  # the supervisor's checks: its own of the API, and the window's
        if answering(machine.tick) is None:
            machine.awake(_PROBE)
            return False
        return True

    def supervisor_pass() -> None:
        with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
                mock.patch.object(supervisor, "_healthy", side_effect=probe):
            supervisor._ensure_api()
            supervisor._reap_window()
            supervisor._ensure_window()
            supervisor._write_state()

    machine.awake(_POLL)
    supervisor_pass()  # the window opens on the API that answers
    [(window_child, window_env)] = popen.of("window")
    assert window_env["VOOL_NATIVE_VOUCHING_PID"] == str(api.pid)
    window_clock = {"now": machine.tick, "check_started": machine.tick}
    window = _installed_watchdog(window_env, tmp_path, monkeypatch, lambda: window_clock["now"])
    assert window.unhealthy_grace == 30.0  # the grace the installed window reads from its environment
    assert window.vouching_pid == api.pid
    # The watchdog asks whether the pid lives when its check starts, before its own probe.
    monkeypatch.setattr(nrs, "pid_alive", lambda pid: popen.stopped_tick.get(pid, math.inf) > window_clock["check_started"])
    monkeypatch.setattr(
        window, "_health", lambda: None if (pid := answering(window_clock["check_started"])) is None else _payload(window, pid)
    )

    hung = machine.tick + 0.5
    events = [(hung + lag, 0, "supervisor"), (hung, 1, "window")]
    lost, lost_at = "", math.inf
    while events:
        at, order, who = heapq.heappop(events)
        if at > hung + 120.0:
            break
        if who == "supervisor":
            machine.at(at)
            supervisor_pass()
            heapq.heappush(events, (machine.tick + _POLL, order, who))
        elif who == "window exits":
            window_child.poll.return_value = 3  # vool_window's exit when its runtime is lost
        else:
            window_clock["check_started"] = at
            window_clock["now"] = at if answering(at) is not None else at + _PROBE
            try:
                window.assert_alive()
            except nrs.NativeRuntimeError as exc:
                lost, lost_at = str(exc), window_clock["now"]
                heapq.heappush(events, (lost_at, 2, "window exits"))
                continue
            heapq.heappush(events, (window_clock["now"] + _WINDOW_POLL, order, who))

    windows = popen.of("window")
    if outcome == "kept":
        assert lost == "", f"the window gave up on a {stall:.0f} s stall: {lost!r}"
        assert "api" not in stops and popen.count("api") == 1
        assert len(windows) == 1
        return
    # The window closed on its own grace, while the pid vouching for it still lived...
    assert lost == "runtime health endpoint is unavailable", lost
    assert lost_at - hung < 40.0
    # ...and reopened once an API answered again.
    assert len(windows) == 2
    reopened_on = windows[1][1]["VOOL_NATIVE_VOUCHING_PID"]
    if outcome == "reopened on the same API":
        assert "api" not in stops and popen.count("api") == 1
        assert reopened_on == str(api.pid)
        return
    assert stops.count("api") == 1
    assert supervisor.last_exit["api"]["reason"].startswith("unresponsive")
    # Restarted by the first of its own checks 40 s or more after its first unanswered one ended
    # (a pass with the window closed runs two probes), after the window had closed.
    restarted = popen.stopped_tick[api.pid]
    assert lost_at < restarted
    assert 40.0 <= restarted - (hung + lag + _PROBE) < 40.0 + _POLL + 2 * _PROBE
    assert popen.count("api") == 2
    assert reopened_on == str(popen.last("api").pid)


def test_two_unanswered_checks_are_never_a_restart_even_across_a_slow_pass(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children, stops: list[str]
) -> None:
    # However long the supervisor's own work makes a pass, two unanswered checks are never a restart.
    # Here the machine is loaded: every step of the pass takes 4 s longer than it should (under the 5 s
    # that would make it a pause), and Ollama, which never answered, is stopped for its startup timeout
    # with the whole 18 s a stop may take. All of it is awake time that counts toward the API's span.
    load = 4.0

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "taskkill.exe":
            machine.awake(8.0)
        return subprocess.CompletedProcess(argv, 0, b"", b"")  # taskkill, or the stale-API scan

    _windows_with(monkeypatch, run, popen)
    _check(supervisor, "ollama", False, popen)
    ollama = popen.last("ollama")

    def wait(timeout: float | None = None) -> int:
        machine.awake(5.0)
        if not ollama.kill.called:
            raise subprocess.TimeoutExpired("ollama.exe", timeout or 0)
        return 0

    ollama.wait.side_effect = wait

    def late(_url: str) -> bool:
        machine.awake(_PROBE + load)
        return False

    _answering_for_a_while(supervisor, machine, "api", popen)
    _passes(supervisor, machine, "api", False, popen, 1)  # the API's first unanswered check
    first_miss = machine.tick
    machine.awake(load)
    supervisor._write_state()  # the pass ends with its state write
    machine.awake(_POLL + load)
    _check(supervisor, "ollama", late, popen)  # Ollama's check, then its stop: taskkill 8 s, 5 s, a kill, 5 s
    machine.awake(load)
    _check(supervisor, "api", late, popen)  # the API's second unanswered check
    assert machine.tick - first_miss == 43.0  # more than the API's 40 s
    # 4899671 fails this here: it restarts the API on its first unanswered check.
    assert stops == ["ollama"]
    assert "paused" not in _log(supervisor)
    _passes(supervisor, machine, "api", False, popen, 1)  # the third
    assert stops == ["ollama", "api"]


def test_a_hung_api_is_still_restarted_when_every_wait_between_passes_is_paused(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _answering_for_a_while(supervisor, machine, "api", popen)
    misses = 0
    while not stops and misses < 40:
        machine.asleep(60.0)  # a VM paused, or Modern Standby, before every pass
        _passes(supervisor, machine, "api", False, popen, 1)
        misses += 1
    # Only each pause's overrun is taken out: every pass still adds its 2 s poll to the span.
    assert stops == ["api"]
    # 4899671 fails this only at misses == 21: it restarts the API on its first miss, so the restart holds there.
    assert misses == 21


def test_ollama_busy_with_a_model_pull_is_given_a_minute_of_unanswered_checks(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _answering_for_a_while(supervisor, machine, "ollama", popen)
    misses = 0
    while not stops and misses < 40:
        _passes(supervisor, machine, "ollama", False, popen, 1)
        misses += 1
    assert stops == ["ollama"]
    assert misses == 19  # misses at 0, 3.5, ..., 63 s: the first 60 s or more in
    assert supervisor.last_exit["ollama"]["reason"].startswith("unresponsive")


def test_a_machine_sleep_between_checks_is_not_counted_as_unanswered_time(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _check(supervisor, "api", False, popen)
    _passes(supervisor, machine, "api", True, popen, 1)
    _passes(supervisor, machine, "api", False, popen, 2)
    machine.asleep(3600)  # the lid closes for an hour between two passes
    _passes(supervisor, machine, "api", False, popen, 1)
    assert stops == []
    assert "paused" in _log(supervisor)
    # Still bounded: a child that stays hung after the resume is restarted after 40 s awake.
    after_resume = 1
    while not stops and after_resume < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        after_resume += 1
    assert stops == ["api"]
    assert after_resume == 11  # 3.5 s of misses before the sleep, its first 2 s poll, then 10 passes
    assert supervisor.last_exit["api"]["reason"].startswith("unresponsive")


def test_a_short_sleep_while_a_check_waits_is_not_counted_as_unanswered_time(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    # Any overrun of more than 5 s is a pause, not only an hour's sleep: here the machine sleeps 7 s (a
    # short standby, or a VM paused for a snapshot) while a check of the hung API waits, 5.5 s past the
    # probe's own 1.5 s.
    _check(supervisor, "api", False, popen)
    _passes(supervisor, machine, "api", True, popen, 1)
    _passes(supervisor, machine, "api", False, popen, 2)

    def check_across_a_short_sleep(_url: str) -> bool:
        machine.asleep(7.0)
        return False

    machine.awake(_PASS)
    _check(supervisor, "api", check_across_a_short_sleep, popen)
    assert stops == []
    # 4899671 fails this here: it has no pauses.
    assert "paused" in _log(supervisor)
    after_resume = 1
    while not stops and after_resume < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        after_resume += 1
    assert stops == ["api"]
    assert after_resume == 10  # 7 s of misses, then that check's 1.5 s of probe, then 9 passes: 40 s awake


@pytest.mark.parametrize("name", ["api", "ollama"])
def test_a_starting_child_keeps_its_startup_minute_across_a_sleep_during_its_check(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str], name: str
) -> None:
    _check(supervisor, name, False, popen)
    _passes(supervisor, machine, name, False, popen, 8)  # 28 s old, still starting

    def check_across_a_sleep(_url: str) -> bool:
        machine.asleep(3600)  # Windows sleeps while this probe waits
        return False

    machine.awake(_PASS)
    _check(supervisor, name, check_across_a_sleep, popen)
    assert stops == []
    for _ in range(12):
        if stops:
            break
        _passes(supervisor, machine, name, False, popen, 1)
    assert stops == [name]  # a child that never answers is still stopped after its awake minute
    assert supervisor.last_exit[name]["reason"] == "startup timeout"


def test_a_clock_set_forward_is_not_counted_as_child_age(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _check(supervisor, "api", False, popen)
    machine.awake(_PASS)
    machine.set_clock(2 * 3600)  # time zone fix, NTP or the user
    _check(supervisor, "api", False, popen)
    assert stops == []
    assert popen.count("api") == 1


def test_a_clock_set_back_does_not_hold_back_a_restart(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    _check(supervisor, "api", False, popen)
    popen.last("api").poll.return_value = 1
    supervisor._reap_dead("api")  # it crashed: retry in 2 s
    machine.set_clock(-24 * 3600)
    supervisor._write_state()
    state = json.loads(supervisor.state_path.read_text(encoding="utf-8"))
    retry_on_the_wall_clock = machine.wall + 2
    _passes(supervisor, machine, "api", False, popen, 1)
    assert popen.count("api") == 2
    # The state file still reports the retry as wall-clock epoch seconds, on the clock as it is now,
    # and a child that was never scheduled as 0.0.
    assert state["next_attempt"]["api"] == pytest.approx(retry_on_the_wall_clock, abs=1e-3)
    assert state["next_attempt"]["ollama"] == 0.0
    assert state["next_attempt"]["window"] == 0.0


def _until_started(supervisor: BundleSupervisor, machine: _Machine, name: str, popen: _Children) -> None:
    before = popen.count(name)
    for _ in range(20):
        if popen.count(name) > before:
            return
        _passes(supervisor, machine, name, False, popen, 1)
    raise AssertionError(f"{name} was never started again")


def _crashes(supervisor: BundleSupervisor, machine: _Machine, name: str, popen: _Children, count: int) -> None:
    for _ in range(count):  # crashes earlier in this session
        _until_started(supervisor, machine, name, popen)
        popen.last(name).poll.return_value = 1
        supervisor._reap_dead(name)


def test_backoff_forgets_old_failures_once_a_child_has_answered_for_a_minute(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    _crashes(supervisor, machine, "ollama", popen, 4)
    assert supervisor.last_exit["ollama"]["retry_in"] == 16
    _until_started(supervisor, machine, "ollama", popen)
    _passes(supervisor, machine, "ollama", True, popen, 9)  # answering for 28 s: not yet
    assert supervisor.failures["ollama"] == 4
    _passes(supervisor, machine, "ollama", True, popen, 10)  # answering for 63 s
    assert supervisor.failures["ollama"] == 0
    popen.last("ollama").poll.return_value = 1
    supervisor._reap_dead("ollama")  # the next crash starts again from the shortest wait
    assert supervisor.last_exit["ollama"]["retry_in"] == 2


def test_a_sleep_between_two_answers_is_not_a_minute_of_answering(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    _crashes(supervisor, machine, "api", popen, 4)
    _until_started(supervisor, machine, "api", popen)
    _passes(supervisor, machine, "api", True, popen, 1)  # it answers...
    machine.asleep(3600)  # ...the lid closes for an hour between two passes...
    _passes(supervisor, machine, "api", True, popen, 1)  # ...and it answers again after the resume
    # 4899671 fails this only at failures == 0 below: it never resets the backoff, so failures stay 4 there.
    assert supervisor.failures["api"] == 4  # 2 s of answering awake, not an hour
    _passes(supervisor, machine, "api", True, popen, 16)  # 58 s awake: not yet
    assert supervisor.failures["api"] == 4
    _passes(supervisor, machine, "api", True, popen, 1)  # a minute of answering awake
    assert supervisor.failures["api"] == 0


def test_an_api_that_answers_once_then_hangs_backs_off_further_each_time(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    retry_in = []
    _check(supervisor, "api", False, popen)
    for cycle in range(4):
        if cycle:
            _until_started(supervisor, machine, "api", popen)
        _passes(supervisor, machine, "api", True, popen, 1)
        stopped, misses = len(stops), 0
        while len(stops) == stopped and misses < 40:
            _passes(supervisor, machine, "api", False, popen, 1)
            misses += 1
        assert misses == 13, f"cycle {cycle}: restarted after {misses} unanswered checks"
        retry_in.append(supervisor.last_exit["api"]["retry_in"])
    # 4899671 fails this at misses == 13 above: it has no answering state, and no reset, so this holds there.
    assert retry_in == [2, 4, 8, 16]


def test_an_answer_to_the_windows_check_gives_a_starting_api_the_rule_of_one_that_answered(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _check(supervisor, "api", False, popen)
    _passes(supervisor, machine, "api", False, popen, 16)  # 56 s old, its own check never answered
    _window_check(supervisor, True, popen)  # 1.5 s after its own check missed, the window's check gets
    assert popen.count("window") == 1  # its first answer, and the window opens on it
    _passes(supervisor, machine, "api", False, popen, 2)  # past its startup minute, two late /healthz
    assert stops == []
    misses = 2
    while not stops and misses < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        misses += 1
    assert stops == ["api"]
    assert misses == 13  # the rule of a child that has answered: three misses over 40 s
    assert supervisor.last_exit["api"]["reason"].startswith("unresponsive")


def test_an_answer_to_the_windows_check_ends_the_apis_run_of_unanswered_checks(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children, stops: list[str]
) -> None:
    _answering_for_a_while(supervisor, machine, "api", popen)
    _window_check(supervisor, True, popen)
    _passes(supervisor, machine, "api", False, popen, 9)  # a 31.5 s stall...
    # 4899671 fails this here, before the claim below: it restarts the API on the stall's first miss.
    assert "api" not in stops
    popen.last("window").poll.return_value = 3  # ...that the window gave up on (exit 3)
    _window_check(supervisor, True, popen)  # the stall is over: the window's check gets an answer
    assert popen.count("window") == 2
    misses = 0
    while "api" not in stops and misses < 40:
        _passes(supervisor, machine, "api", False, popen, 1)  # then the API hangs
        misses += 1
    assert misses == 13  # 40 s counted from the first miss after that answer, not the stall's
    assert supervisor.last_exit["api"]["reason"].startswith("unresponsive")


def test_a_machine_sleep_while_vool_starts_does_not_restart_its_api(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
    stops: list[str],
) -> None:
    api_answers = {"now": False}

    def healthy(url: str) -> bool:
        return ":11434/" in url or api_answers["now"]

    def after_sleep(n: int) -> None:
        if n == 2:
            machine.asleep(3600)  # the lid closes during the second wait
        if n == 3:
            api_answers["now"] = True
        _stop_after(supervisor, n, 5)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert popen.count("api") == 1
    assert popen.count("window") == 1
    assert stops == ["window", "api", "ollama"]  # only the stop request's own teardown
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "paused" in log


def test_a_machine_sleep_during_the_windows_check_does_not_restart_the_starting_api(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    # Until the API first answers, every pass also runs the window's own check of it: at first
    # launch, and after every exit 3 until a new API answers. A sleep that starts while that check
    # waits is as much a pause as one in the poll wait.
    in_windows_check = {"now": False}
    real_ensure_window = supervisor._ensure_window

    def ensure_window() -> None:
        in_windows_check["now"] = True
        try:
            real_ensure_window()
        finally:
            in_windows_check["now"] = False

    monkeypatch.setattr(supervisor, "_ensure_window", ensure_window)
    api = {"answers": False, "slept": False}

    def healthy(url: str) -> bool:
        if ":11434/" in url:
            return True
        if in_windows_check["now"] and not api["slept"]:
            api["slept"] = True
            machine.asleep(3600)  # the lid closes while the window's check waits
        return api["answers"]

    def after_sleep(n: int) -> None:
        if n == 4:
            api["answers"] = True  # the API is up after 8 s awake
        _stop_after(supervisor, n, 6)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert api["slept"]
    assert popen.count("api") == 1
    assert popen.count("window") == 1
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "during the window's health check" in log  # the pause is found in the check it fell in


def test_a_sleep_before_a_child_is_found_exited_is_not_counted_as_the_starting_apis_age(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    def healthy(url: str) -> bool:
        if ":11434/" in url:
            ollama = supervisor.processes.get("ollama")
            return ollama is not None and ollama.poll() is None
        return False  # the API is still starting

    def after_sleep(n: int) -> None:
        if n == 23:
            machine.asleep(3600)  # the lid closes during the 23rd wait, 46 s into the API's startup minute...
            popen.last("ollama").poll.return_value = 2  # ...and Ollama exits while the machine sleeps
        _stop_after(supervisor, n, 26)

    # The next pass finds Ollama exited and stops it before any health check.
    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert popen.count("ollama") == 2  # the exited Ollama is started again
    assert popen.count("api") == 1
    log = _log(supervisor)
    assert "ollama retry scheduled reason=exited returncode=2" in log
    assert "startup timeout" not in log
    assert "paused" in log


def test_a_sleep_before_a_child_is_found_exited_is_not_counted_as_ollamas_unanswered_time(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    ollama_busy = {"now": False}

    def healthy(url: str) -> bool:
        if ":11434/" in url:
            return "ollama" in supervisor.processes and not ollama_busy["now"]
        api = supervisor.processes.get("api")
        return api is not None and api.poll() is None

    def after_sleep(n: int) -> None:
        if n == 2:
            ollama_busy["now"] = True  # Ollama, which has answered, loads a model: /api/tags is late
        if n == 4:
            machine.asleep(3600)  # two late answers in, the lid closes...
            popen.last("api").poll.return_value = 1  # ...and the API exits while the machine sleeps
        _stop_after(supervisor, n, 6)

    # The next pass finds the API exited and stops it before Ollama's third late answer.
    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert popen.count("ollama") == 1
    log = _log(supervisor)
    assert "api retry scheduled reason=exited returncode=1" in log
    assert "ollama retry scheduled" not in log
    assert "paused" in log


# The supervisor's own work -- a stop, a start, the state write, the stale-API scan -- is awake time
# up to its own limit, however slow: a sleep that starts inside it is a pause beyond that limit.


def test_a_sleep_while_a_child_is_stopped_is_not_counted_as_the_starting_apis_age(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    # Ollama never answers and is stopped for its startup timeout a minute in. The machine sleeps
    # while that stop waits for Ollama to exit, and the API, started again after a crash, is still
    # starting: its own minute is not over.
    slept = {"in_stop": False}

    def wait_across_a_sleep(timeout: float | None = None) -> int:
        if not slept["in_stop"]:
            slept["in_stop"] = True
            machine.asleep(3600)  # the lid closes while the stop waits
        return 0

    def healthy(url: str) -> bool:
        # Ollama never answers; the API answers from 30 s awake after its second start.
        return ":11435/" in url and "api" in supervisor.processes and machine.sleeps >= 34

    def after_sleep(n: int) -> None:
        if n == 1:
            popen.last("ollama").wait.side_effect = wait_across_a_sleep
        if n == 18:
            popen.last("api").poll.return_value = 1  # the first API crashes; the next one starts at 38 s
        _stop_after(supervisor, n, 36)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert slept["in_stop"]
    assert popen.count("api") == 2  # the crash's restart, and no other
    log = _log(supervisor)
    assert "ollama retry scheduled reason=startup timeout" in log
    assert "api retry scheduled reason=startup timeout" not in log
    assert "while stopping ollama" in log


def test_a_sleep_while_a_child_is_started_is_not_counted_as_any_childs_age(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    def start_across_a_sleep(argv: list[str], **kwargs: object) -> mock.Mock:
        child = popen(argv, **kwargs)
        if any(str(part).endswith(_PROGRAM["api"]) for part in argv):
            machine.asleep(3600)  # the lid closes while Windows creates the API's process
        return child

    def healthy(url: str) -> bool:
        # Each answers once it runs, Ollama 4 s and the API 6 s awake after the resume.
        name = "ollama" if ":11434/" in url else "api"
        return name in supervisor.processes and machine.sleeps >= (2 if name == "ollama" else 3)

    _run(supervisor, machine, monkeypatch, start_across_a_sleep, healthy, lambda n: _stop_after(supervisor, n, 6))
    assert popen.count("ollama") == 1  # started just before it, and not aged by the sleep
    assert popen.count("api") == 1  # its age starts once its process exists
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "while starting api" in log


class _StateFile:
    """The supervisor's state file; the machine sleeps during its next write once that is armed."""

    def __init__(self, path: Path, machine: _Machine) -> None:
        self.path = path
        self.machine = machine
        self.sleep_in_next_write = False

    def write_text(self, text: str, encoding: str) -> int:
        if self.sleep_in_next_write:
            self.sleep_in_next_write = False
            self.machine.asleep(3600)  # the lid closes while the state is written
        return self.path.write_text(text, encoding=encoding)

    def unlink(self, missing_ok: bool = False) -> None:
        self.path.unlink(missing_ok=missing_ok)


def test_a_sleep_while_the_state_is_written_is_not_counted_as_the_starting_apis_age(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    state = _StateFile(supervisor.state_path, machine)
    monkeypatch.setattr(supervisor, "state_path", state)

    def healthy(url: str) -> bool:
        return ":11434/" in url or machine.sleeps >= 5  # the API answers 10 s awake after its start

    def after_sleep(n: int) -> None:
        if n == 2:
            state.sleep_in_next_write = True  # the write that ends the third pass
        _stop_after(supervisor, n, 7)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert not state.sleep_in_next_write
    assert popen.count("api") == 1
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "while writing the state file" in log


def test_a_stop_that_takes_its_whole_18_s_is_awake_time(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children, stops: list[str]
) -> None:
    # A slow but real stop is no pause: here Windows stops a hung Ollama's tree, with taskkill's 8 s, 5 s
    # for Ollama to exit, a kill and 5 s more, while the API, which has answered, goes unanswered.
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "taskkill.exe":
            machine.awake(8.0)
        return subprocess.CompletedProcess(argv, 0, b"", b"")  # taskkill, or the stale-API scan

    _windows_with(monkeypatch, run, popen)
    _check(supervisor, "ollama", False, popen)
    ollama = popen.last("ollama")

    def wait(timeout: float | None = None) -> int:
        machine.awake(5.0)
        if not ollama.kill.called:
            raise subprocess.TimeoutExpired("ollama.exe", timeout or 0)
        return 0

    ollama.wait.side_effect = wait
    _answering_for_a_while(supervisor, machine, "api", popen)
    _passes(supervisor, machine, "api", False, popen, 1)  # the API's first unanswered check
    # 4899671 fails this at misses == 8 below: it restarts the API on this first miss (and counts all own work awake).
    stop_started = machine.tick
    supervisor._stop_child("ollama")
    assert machine.tick - stop_started == 18.0
    misses = 1
    while "api" not in stops and misses < 40:
        _passes(supervisor, machine, "api", False, popen, 1)
        misses += 1
    assert misses == 8  # 18 s of the stop and 7 passes of 3.5 s: the API's 40 s, counted awake
    assert "paused" not in _log(supervisor)


def test_a_stale_api_scan_that_takes_its_whole_30_s_is_awake_time(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children, stops: list[str]
) -> None:
    # A slow but real scan is no pause: here WMI takes the scan's whole 30 s before the API's start,
    # while Ollama, which has answered, goes unanswered.
    monkeypatch.setattr(supervisor, "_clear_stale_api_processes", lambda: machine.awake(30.0))
    _answering_for_a_while(supervisor, machine, "ollama", popen)
    _passes(supervisor, machine, "ollama", False, popen, 1)  # Ollama's first unanswered check
    # 4899671 fails this at misses == 10 below: it restarts Ollama on this first miss (and counts all own work awake).
    scan_started = machine.tick
    _check(supervisor, "api", False, popen)  # the scan, then the API's start
    assert machine.tick - scan_started == 30.0 and popen.count("api") == 1
    misses = 1
    while "ollama" not in stops and misses < 40:
        _passes(supervisor, machine, "ollama", False, popen, 1)
        misses += 1
    assert misses == 10  # 30 s of the scan and 9 passes of 3.5 s: Ollama's 60 s, counted awake
    assert "paused" not in _log(supervisor)


# Each span is measured once, from where the one before it ended, and ages and spans start at such an
# instant: a sleep that starts between two of the supervisor's steps (while it logs, say) is a pause
# taken out once, neither twice nor not at all.


def _sleep_in_the_log_line(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, starts: Callable[[str], bool]
) -> list[float]:
    """The lid closes for an hour while the supervisor writes the first log line `starts` picks; the
    tick that write began at is recorded."""
    real_log = supervisor._log
    slept: list[float] = []

    def log(message: str) -> None:
        if not slept and starts(message):
            slept.append(machine.tick)
            machine.asleep(3600)
        real_log(message)

    monkeypatch.setattr(supervisor, "_log", log)
    return slept


def test_a_sleep_while_a_hung_childs_first_miss_is_logged_does_not_hold_back_its_restart(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # A guard: 4899671 passes this too, as it restarts Ollama on its first unanswered check. Ollama
    # answers for 70 s, then hangs for good, and the lid closes while its first miss is logged. That
    # hour, taken out twice, would keep the hung Ollama for another hour awake.
    def first_miss(message: str) -> bool:
        return machine.sleeps >= 35 and message.startswith("health ollama=unhealthy")

    slept = _sleep_in_the_log_line(supervisor, machine, monkeypatch, first_miss)

    def healthy(url: str) -> bool:
        if ":11434/" in url:
            return "ollama" in supervisor.processes and machine.sleeps < 35
        return "api" in supervisor.processes

    def after_sleep(n: int) -> None:
        if popen.count("ollama") == 2 or n == 100:
            supervisor.stop_path.write_text("stop\n", encoding="utf-8")

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert slept
    hung = popen.of("ollama")[0][0]
    assert popen.count("ollama") == 2  # the hung Ollama was restarted...
    # ...within its 60 s of unanswered checks awake after its first miss, not an hour later
    assert popen.stopped_tick[hung.pid] - slept[0] - 3600 <= 60.0 + _POLL


def test_a_sleep_while_a_crash_is_logged_is_not_counted_as_ollamas_startup_age(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # The API crashes while Ollama, which answers 40 s awake after its start, is still starting. The lid
    # closes while the API's retry is logged, after the stop of the crashed API and before the pass's
    # state write: that write measures from where the stop ended, so the hour is a pause too.
    def the_apis_retry(message: str) -> bool:
        return message.startswith("api retry scheduled")

    slept = _sleep_in_the_log_line(supervisor, machine, monkeypatch, the_apis_retry)

    def healthy(url: str) -> bool:
        if ":11434/" in url:
            return "ollama" in supervisor.processes and machine.sleeps >= 20
        api = supervisor.processes.get("api")
        return api is not None and api.poll() is None

    def after_sleep(n: int) -> None:
        if n == 5:
            popen.last("api").poll.return_value = 1  # the API crashes
        _stop_after(supervisor, n, 25)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert slept
    assert popen.count("ollama") == 1  # still starting after the sleep, and not aged by it
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "paused" in log


def test_a_sleep_while_a_pause_in_the_apis_start_is_logged_is_taken_out_of_its_age_once(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # The API never answers. The lid closes while Windows creates its process, and again while that
    # pause is logged, before its age starts: neither hour is its age, and the second, taken out twice,
    # would keep the hung API for another hour awake.
    def start_across_a_sleep(argv: list[str], **kwargs: object) -> mock.Mock:
        child = popen(argv, **kwargs)
        if popen.count("api") == 1 and any(str(part).endswith(_PROGRAM["api"]) for part in argv):
            machine.asleep(3600)  # the lid closes while Windows creates the first API's process
        return child

    def the_pause_in_its_start(message: str) -> bool:
        return "while starting api" in message

    slept = _sleep_in_the_log_line(supervisor, machine, monkeypatch, the_pause_in_its_start)

    def healthy(url: str) -> bool:
        return ":11434/" in url  # Ollama answers; the API never does

    def after_sleep(n: int) -> None:
        if popen.count("api") == 2 or n == 100:
            supervisor.stop_path.write_text("stop\n", encoding="utf-8")

    _run(supervisor, machine, monkeypatch, start_across_a_sleep, healthy, after_sleep)
    hung = popen.of("api")[0][0]
    awake_age = popen.stopped_tick[hung.pid] - popen.started_tick[hung.pid] - 2 * 3600
    # 4899671 fails this here: it counts the first hour as the API's age and stops it at once.
    assert awake_age >= 60.0 - _POLL  # its startup minute, counted awake from its start...
    assert awake_age <= 60.0 + _PASS  # ...and not an hour more
    assert slept
    assert popen.count("api") == 2
    assert "api retry scheduled reason=startup timeout" in _log(supervisor)


def test_a_sleep_while_a_pause_in_the_windows_check_is_logged_is_taken_out_of_the_apis_answering_once(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # The first API crashes. The second answers first to the window's check: the lid closes while that
    # check waits, and again while the pause it found is logged. Its minute of answering, after which
    # the restart backoff forgets the crash, starts after both: the second hour, taken out twice, would
    # hold that reset back an hour awake.
    in_windows_check = {"now": False}
    real_ensure_window = supervisor._ensure_window

    def ensure_window() -> None:
        in_windows_check["now"] = True
        try:
            real_ensure_window()
        finally:
            in_windows_check["now"] = False

    monkeypatch.setattr(supervisor, "_ensure_window", ensure_window)

    def the_pause_in_the_windows_check(message: str) -> bool:
        return "during the window's health check" in message

    slept = _sleep_in_the_log_line(supervisor, machine, monkeypatch, the_pause_in_the_windows_check)
    resets: list[float] = []
    logged = supervisor._log

    def log(message: str) -> None:
        if message.startswith("api answering for"):
            resets.append(machine.tick)
        logged(message)

    monkeypatch.setattr(supervisor, "_log", log)
    answered: list[float] = []

    def healthy(url: str) -> bool:
        if ":11434/" in url or answered:
            return True
        if in_windows_check["now"] and popen.count("api") == 2:
            machine.asleep(3600)  # the lid closes while the window's check waits for the API's first answer
            answered.append(machine.tick)
            return True
        return False

    def after_sleep(n: int) -> None:
        if n == 3:
            popen.last("api").poll.return_value = 1  # the first API crashes
        if resets or n == 100:
            supervisor.stop_path.write_text("stop\n", encoding="utf-8")

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    # 4899671 fails this only at failures == 0: it never resets the backoff, so failures stay 1 there.
    assert supervisor.failures["api"] == 0  # the backoff forgot the crash...
    assert 60.0 <= resets[0] - answered[0] - 3600 <= 60.0 + _POLL  # ...after a minute of answering awake
    assert slept
    assert popen.count("api") == 2
    assert popen.count("window") == 1


# The probe asks the child itself: no proxy, no redirect, and an answer that is not HTTP is an
# unhealthy child, not an error that ends the supervisor. Only a 2xx answer is healthy; a redirect or
# any other status is unhealthy.


@contextlib.contextmanager
def _http_stub(status: int, headers: dict[str, str] | None = None) -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.requestline)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


_PROBE_FROM_A_FRESH_SUPERVISOR = (
    "import json, sys\n"
    "from installer.bundle.bundle_supervisor import BundleSupervisor\n"
    "probe = object.__new__(BundleSupervisor)\n"
    "print(json.dumps([probe._healthy(url) for url in sys.argv[1:]]))\n"
)


def test_the_health_check_goes_straight_to_127_0_0_1_past_a_configured_proxy() -> None:
    # A supervisor process started with a proxy configured, as installed: urllib reads the proxy
    # settings (HTTP_PROXY here; the Windows Internet settings reach it the same way) when it builds
    # its handlers, at import or on first use, so the probe runs in a fresh interpreter.
    with _http_stub(200) as (child, child_hits), _http_stub(404) as (missing, missing_hits), \
            _http_stub(503) as (failing, failing_hits), _http_stub(502) as (proxy, proxy_hits):
        env = {key: value for key, value in os.environ.items() if key.lower() not in {"no_proxy", "http_proxy"}}
        env["http_proxy"] = proxy
        probe = subprocess.run(
            [sys.executable, "-c", _PROBE_FROM_A_FRESH_SUPERVISOR, f"{child}/healthz", f"{missing}/healthz", f"{failing}/healthz"],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert probe.returncode == 0, probe.stderr
    healthy, missing_healthy, failing_healthy = json.loads(probe.stdout.strip().splitlines()[-1])
    assert proxy_hits == []
    assert child_hits and healthy is True
    # Only a 2xx answer is healthy: a 404 or a 503 from the child itself is unhealthy through the same
    # opener (tests/test_bundle_tooling.py pins the 404 rule with a mock of the opener this no longer uses).
    assert missing_hits and missing_healthy is False
    assert failing_hits and failing_healthy is False


def test_the_health_check_does_not_follow_a_redirect_away_from_the_child(supervisor: BundleSupervisor) -> None:
    with _http_stub(200) as (elsewhere, elsewhere_hits), \
            _http_stub(302, {"Location": f"{elsewhere}/healthz"}) as (child, child_hits):
        healthy = supervisor._healthy(f"{child}/healthz")
    assert child_hits
    assert elsewhere_hits == []
    assert healthy is False  # a redirect is unhealthy, whatever it points at


@contextlib.contextmanager
def _not_http_stub() -> Iterator[tuple[str, list[bytes]]]:
    """A program that is not an HTTP server on 127.0.0.1. It accepts one connection, reads the whole
    request, records it, and answers with a line that is not HTTP."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    requests: list[bytes] = []

    def answer_once() -> None:
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2.0)
                request = b""
                # Read the whole request first: closing on unread bytes is a reset on Windows.
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                requests.append(request)
                connection.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")

    thread = threading.Thread(target=answer_once, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/healthz", requests
    finally:
        listener.close()
        thread.join(5)


def test_a_port_answered_by_something_that_is_not_http_is_unhealthy(supervisor: BundleSupervisor) -> None:
    with _not_http_stub() as (url, requests):
        healthy = _outcome(lambda: supervisor._healthy(url))
    # The stub was reached and read the whole probe: the answer judged is its, not a dead port's.
    assert len(requests) == 1 and requests[0].startswith(b"GET /healthz HTTP/1.1\r\n")
    assert requests[0].endswith(b"\r\n\r\n")
    assert healthy is False


def test_a_non_http_answer_on_the_child_ports_does_not_end_the_supervisor(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    _run_without_the_lease(supervisor, monkeypatch)

    def no_request(self: http.client.HTTPConnection, *_: object, **__: object) -> None:
        return None

    def not_http(self: http.client.HTTPConnection) -> None:
        raise http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6\r\n")

    # Every probe -- 11434, 11435 -- gets an answer that is not HTTP; nothing touches the network.
    monkeypatch.setattr(http.client.HTTPConnection, "request", no_request)
    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", not_http)
    machine.after_sleep = lambda n: n >= 3 and supervisor.stop_path.write_text("stop\n", encoding="utf-8")
    with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen):
        assert _outcome(supervisor.run) == 0
    assert machine.sleeps == 3
    assert popen.count("ollama") == 1 and popen.count("api") == 1


# The stale-API PowerShell scan runs once per API start attempt, never for long, keeps the output it
# never reads as bytes, and is awake time only up to its 30 s limit.


def test_the_stale_api_scan_runs_once_per_start_attempt_and_has_a_timeout(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[tuple[list[str], dict]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        scans.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def popen(*_: object, **__: object) -> None:
        raise OSError("the API would not start")

    _windows_with(monkeypatch, run, popen)
    with mock.patch.object(supervisor, "_healthy", return_value=False):
        supervisor._ensure_api()  # scan, the start fails, back off 2 s
        for _ in range(5):
            machine.awake(0.3)
            supervisor._ensure_api()
        assert len(scans) == 1
        machine.awake(2.0)
        supervisor._ensure_api()
    assert len(scans) == 2
    assert all(argv[0] == "powershell.exe" for argv, _ in scans)
    assert all(0 < float(kwargs.get("timeout") or 0) <= 60 for _, kwargs in scans)


def test_a_hung_stale_api_scan_is_given_up_and_the_api_still_starts(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    def run(argv: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    _windows_with(monkeypatch, run, popen)
    with mock.patch.object(supervisor, "_healthy", return_value=False):
        assert _outcome(supervisor._ensure_api) is None
    assert popen.count("api") == 1
    assert "stale api cleanup gave up" in _log(supervisor)


def test_the_stale_api_scan_keeps_the_output_it_never_reads_as_bytes(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    """PowerShell writes in the console's code page. These bytes are text in neither UTF-8 (the
    bundled Python runs with PYTHONUTF8=1) nor cp1252. Decoded as text, they raise UnicodeDecodeError
    out of subprocess.run on POSIX; on Windows subprocess's reader thread swallows that error and
    the output is lost. So the real-child check below discriminates only on POSIX, and the scan's
    run options are pinned directly too."""
    output = b"\x81\x8d\x8f\x90\x9d\r\n"
    scans: list[tuple[list[str], dict]] = []

    def recorded(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        scans.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, output, b"")

    _windows_with(monkeypatch, recorded, popen)
    with mock.patch.object(supervisor, "_healthy", return_value=False):
        supervisor._ensure_api()
    [(argv, kwargs)] = scans
    assert argv[0] == "powershell.exe"
    decoding = {key: kwargs[key] for key in ("text", "encoding", "errors", "universal_newlines") if kwargs.get(key)}
    assert decoding == {}
    assert popen.count("api") == 1

    # A real Python child writes the bytes; the supervisor's own run options say how they are read.
    read: list[object] = []

    def real_child(argv: list[str], **kwargs: object) -> object:
        completed = subprocess.run([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({output!r})"], **kwargs)
        read.append(completed.stdout)
        return completed

    _windows_with(monkeypatch, real_child, popen)
    supervisor._clear_stale_api_processes()
    assert read == [output]


def test_a_sleep_during_the_stale_api_scan_is_not_counted_as_ollamas_startup_age(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    # At launch Ollama is started first, then the scan runs before the API's start. The scan is our
    # own work, awake time up to its 30 s limit: a sleep that starts inside it is a pause.
    scans: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "powershell.exe":
            scans.append(argv)
            if len(scans) == 1:
                machine.asleep(3600)  # the lid closes while PowerShell asks WMI for stale APIs
        return subprocess.CompletedProcess(argv, 0, b"", b"")  # the scan, or taskkill at the stop request

    _windows_with(monkeypatch, run, popen)

    def healthy(url: str) -> bool:
        # Each answers once it runs, Ollama 4 s and the API 6 s awake after the resume.
        name = "ollama" if ":11434/" in url else "api"
        return name in supervisor.processes and machine.sleeps >= (2 if name == "ollama" else 3)

    _run(supervisor, machine, monkeypatch, popen, healthy, lambda n: _stop_after(supervisor, n, 5))
    assert len(scans) == 1
    assert popen.count("ollama") == 1
    assert popen.count("api") == 1
    log = _log(supervisor)
    assert "startup timeout" not in log
    assert "during the stale-API scan" in log


# A window the supervisor reopened after its runtime was lost that exits 1 within its first minute
# awake never attached: its host's one readiness check of the API missed. It is tried again, with the
# restart backoff, once the API answers, six times at most. Its life runs up to where its exit is first
# seen, not to the pass that finds it exited. Any other exit of it, a later exit 1 (a forced end), and
# an exit 1 of any other window, stays closed.


def test_a_window_reopened_on_an_api_that_answers_unevenly_is_opened_again_until_it_attaches(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
) -> None:
    """The API stalls for over half a minute, then answers again, unevenly at first. The window gives
    up on its own 30 s grace (exit 3); the API, under its 40 s, is kept; the window is reopened on it.
    The reopened window's host checks the API once before it attaches, that check lands on a late
    answer, and the host exits 1 without showing a window. That is no close by the user: the window
    is tried again once its 2 s retry is due, and attaches to the same API."""
    reaped = {"at": math.inf}

    def healthy(url: str) -> bool:
        answers = ":11434/" in url or (machine.sleeps >= 1 and not 2 <= machine.sleeps < 12)
        if not answers:
            machine.awake(_PROBE)  # the probe waits out its timeout
        return answers

    def after_sleep(n: int) -> None:
        windows = [child for child, _ in popen.of("window")]
        if n == 11:
            windows[0].poll.return_value = 3  # 31.5 s into the stall, the window gives up on it
        if len(windows) == 2 and windows[1].poll.return_value is None:
            windows[1].poll.return_value = 1  # the reopened window's one readiness check missed
            reaped["at"] = machine.tick  # the next pass finds it exited
        _stop_after(supervisor, n, 20)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    windows = popen.of("window")
    assert len(windows) == 3
    api = popen.last("api")
    assert popen.count("api") == 1
    assert [env["VOOL_NATIVE_VOUCHING_PID"] for _, env in windows] == [str(api.pid)] * 3
    attached = windows[2][0]
    assert popen.started_tick[attached.pid] - reaped["at"] >= 2.0  # after the restart backoff
    assert popen.stopped_tick[attached.pid] == machine.tick  # open until the stop request's teardown
    assert supervisor.last_exit["window"]["retry_in"] == 2
    log = _log(supervisor)
    assert "the reopened window did not attach to the API (returncode=1)" in log
    assert "unresponsive" not in log and "startup timeout" not in log


def _window_passes(supervisor: BundleSupervisor, machine: _Machine, popen: _Children, count: int) -> None:
    """`count` passes of the supervisor loop's window steps, each after its 2 s poll, with the API
    answering: reap a window that exited, honor a relaunch, then the window's own check of the API."""
    for _ in range(count):
        machine.awake(_POLL)
        with mock.patch("installer.bundle.bundle_supervisor.subprocess.Popen", popen), \
                mock.patch.object(supervisor, "_healthy", return_value=True):
            supervisor._reap_window()
            supervisor._consume_window_reopen()
            supervisor._ensure_window()


def _awake_until_a_window_opens(supervisor: BundleSupervisor, machine: _Machine, popen: _Children) -> float:
    """Window passes until another window opens: the awake time from the first of them, which finds
    the last window exited, to that opening (math.inf if none opens within a minute)."""
    opened, reaped_at = popen.count("window"), machine.tick + _POLL
    for _ in range(30):
        _window_passes(supervisor, machine, popen, 1)
        if popen.count("window") > opened:
            return popen.started_tick[popen.last("window").pid] - reaped_at
    return math.inf


def _with_a_window(supervisor: BundleSupervisor, machine: _Machine, popen: _Children) -> None:
    _check(supervisor, "api", False, popen)  # our API, which answers every check from here on
    _window_passes(supervisor, machine, popen, 1)
    assert popen.count("window") == 1


def test_a_first_window_that_exits_1_stays_closed(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    # The rule this guards is unchanged, so this test passes on 4899671 too: only a window reopened
    # after a runtime loss is tried again.
    _with_a_window(supervisor, machine, popen)
    popen.last("window").poll.return_value = 1  # it never attached
    assert _awake_until_a_window_opens(supervisor, machine, popen) == math.inf
    assert popen.count("window") == 1
    assert "window" not in supervisor.last_exit


def test_a_reopened_window_the_user_closes_stays_closed(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    _with_a_window(supervisor, machine, popen)
    popen.last("window").poll.return_value = 3  # it loses its runtime
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0  # reopened at once
    popen.last("window").poll.return_value = 1  # the reopened window does not attach
    # 4899671 fails this test here, before its claim: it never tries a reopened window again.
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 2.0
    popen.last("window").poll.return_value = 0  # the user closes it
    assert _awake_until_a_window_opens(supervisor, machine, popen) == math.inf
    assert popen.count("window") == 3
    # Its close ends the reopen: a window the user's next launch opens, that exits 1, stays closed.
    supervisor._request_window_reopen()
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0
    popen.last("window").poll.return_value = 1
    assert _awake_until_a_window_opens(supervisor, machine, popen) == math.inf
    assert popen.count("window") == 4


def test_each_runtime_loss_starts_the_retries_of_the_reopened_window_over(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    _with_a_window(supervisor, machine, popen)
    waits = []
    for misses in (2, 1):  # the window reopened after the first loss misses twice, after the second once
        popen.last("window").poll.return_value = 3  # the window loses its runtime
        assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0
        for _ in range(misses):
            popen.last("window").poll.return_value = 1  # the reopened window does not attach
            waits.append(_awake_until_a_window_opens(supervisor, machine, popen))
            assert waits[-1] < math.inf  # 4899671 fails this test here: it never tries it again
    # The restart backoff, started over by the second loss: the window that lost its runtime had attached.
    assert waits == [2.0, 4.0, 2.0]


def test_a_reopened_window_that_never_attaches_is_opened_six_more_times_then_stays_closed(
    supervisor: BundleSupervisor, machine: _Machine, popen: _Children
) -> None:
    # After a runtime loss another VOOL (another Windows user's, or another profile's) can win the API's
    # port and answer the supervisor's checks; every window host refuses that runtime and exits 1 at
    # once without showing a window. That is tried for about 90 s, not for as long as it lasts.
    _with_a_window(supervisor, machine, popen)
    popen.last("window").poll.return_value = 3  # it loses its runtime
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0
    waits = []
    for _ in range(7):
        popen.last("window").poll.return_value = 1  # its host refuses the runtime on the port
        waits.append(_awake_until_a_window_opens(supervisor, machine, popen))
    # 4899671 fails this here: it never opens a reopened window that exits 1 again.
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, math.inf]
    _window_passes(supervisor, machine, popen, 300)  # ten minutes more
    assert popen.count("window") == 8
    # The user's next launch opens it again; once that window has attached, its runtime loss starts
    # the tries over.
    supervisor._request_window_reopen()
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0
    popen.last("window").poll.return_value = 3
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 0.0
    popen.last("window").poll.return_value = 1
    assert _awake_until_a_window_opens(supervisor, machine, popen) == 2.0


def _a_reopened_window_exits_1(
    supervisor: BundleSupervisor,
    machine: _Machine,
    monkeypatch: pytest.MonkeyPatch,
    popen: _Children,
    open_for: float,
    lid: str = "",
    missed_first: bool = False,
) -> float:
    """Through run(): the API stalls for over half a minute and the window gives up on it (exit 3);
    the window reopened on the same API, which answers from then on, attaches -- or, if
    `missed_first`, that window's one check of the API misses (exit 1 at once) and the window tried
    again attaches. Its host exits 1 once it has been open `open_for` seconds awake, and the run goes
    on for another minute. The lid closes for an hour after that exit if `lid` says where: "after the
    exit", "during the next check", or "before the reap" (as the supervisor turns to the window).
    Returns the tick it exited at (math.inf if no window was open that long)."""
    attached = 2 if missed_first else 1  # the window that attaches
    ended = {"at": math.inf, "pass": math.inf}
    slept: list[float] = []

    def lid_closes(where: str) -> None:
        if lid == where and ended["at"] < math.inf and not slept:
            slept.append(machine.tick)
            machine.asleep(3600)

    real_reap_window = supervisor._reap_window

    def reap_window() -> None:
        lid_closes("before the reap")
        real_reap_window()

    monkeypatch.setattr(supervisor, "_reap_window", reap_window)

    def healthy(url: str) -> bool:
        lid_closes("during the next check")
        answers = ":11434/" in url or (machine.sleeps >= 1 and not 2 <= machine.sleeps < 12)
        if not answers:
            machine.awake(_PROBE)  # the probe waits out its timeout
        return answers

    def after_sleep(n: int) -> None:
        windows = [child for child, _ in popen.of("window")]
        if n == 11:
            windows[0].poll.return_value = 3  # 31.5 s into the stall, the window gives up on it
        if missed_first and len(windows) == 2:
            windows[1].poll.return_value = 1  # the reopened window's one readiness check missed
        open_now = machine.tick - popen.started_tick[windows[attached].pid] if len(windows) == attached + 1 else 0.0
        if ended["at"] == math.inf and open_now >= open_for:
            windows[attached].poll.return_value = 1
            ended.update({"at": machine.tick, "pass": n})
            lid_closes("after the exit")
        _stop_after(supervisor, n, int(min(ended["pass"], 100)) + 30)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    assert popen.count("api") == 1
    windows = popen.of("window")
    assert [env["VOOL_NATIVE_VOUCHING_PID"] for _, env in windows] == [str(popen.last("api").pid)] * len(windows)
    if ended["at"] < math.inf:
        assert popen.started_tick[windows[attached][0].pid] + open_for == ended["at"]
        assert len(slept) == (1 if lid else 0)
    return ended["at"]


@pytest.mark.parametrize(
    "lid", [pytest.param("", id="awake"), pytest.param("during the next check", id="asleep-after-the-exit")]
)
def test_a_reopened_window_that_attached_and_is_ended_with_exit_1_stays_closed(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children, lid: str
) -> None:
    # A guard: 4899671 passes this too, as it never opens a window that exits 1 again. Exit 1 after the
    # window's first minute is a forced end (taskkill /F) of a window that had attached: the user's. An
    # hour's sleep during the check after that exit is taken out of its start and its exit alike.
    ended = _a_reopened_window_exits_1(supervisor, machine, monkeypatch, popen, open_for=62.0, lid=lid)
    assert ended < math.inf
    assert popen.count("window") == 2
    assert "window retry scheduled" not in _log(supervisor)


def test_a_reopened_window_tried_again_that_attached_and_is_ended_with_exit_1_stays_closed(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # The reopened window's one check misses and it is tried again. The window tried again attaches, and
    # its exit 1 after its first minute is a forced end: the exit of the window before it is no part of
    # its life.
    ended = _a_reopened_window_exits_1(supervisor, machine, monkeypatch, popen, open_for=62.0, missed_first=True)
    # 4899671 fails this here, before its claim: it never tries the reopened window that missed again.
    assert popen.count("window") == 3
    assert ended < math.inf
    assert _log(supervisor).count("window retry scheduled") == 1


@pytest.mark.parametrize(
    "lid", [pytest.param("before the reap", id="asleep-before-the-reap"), pytest.param("after the exit", id="asleep-after-the-exit")]
)
def test_a_reopened_window_that_exits_1_within_its_first_minute_awake_is_opened_again(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children, lid: str
) -> None:
    # Its host exits 1 a full minute awake after its start, and the lid closes for an hour, right after
    # that exit or just before the supervisor finds it exited: that hour is no part of the window's life.
    ended = _a_reopened_window_exits_1(supervisor, machine, monkeypatch, popen, open_for=60.0, lid=lid)
    windows = popen.of("window")
    # 4899671 fails this here: it never opens a window that exits 1 again.
    assert len(windows) == 3
    attached = windows[2][0]
    assert popen.started_tick[attached.pid] - ended - 3600 == 2.0  # after its 2 s retry, awake
    assert popen.stopped_tick[attached.pid] == machine.tick  # open until the stop request's teardown
    assert "paused" in _log(supervisor)


def test_a_reopened_window_that_exits_1_at_once_is_opened_again_however_long_the_pass_that_finds_it(
    supervisor: BundleSupervisor, machine: _Machine, monkeypatch: pytest.MonkeyPatch, popen: _Children
) -> None:
    # The window loses its runtime (exit 3) and is reopened. The API then crashes under the reopened
    # window, whose host, finding no API, exits 1 four seconds after its start. The pass after that is
    # long, all of it awake time within the code's own limits: each 2 s wait wakes 3 s late on a loaded
    # machine, a hung Ollama's probe waits out both its 1.5 s steps and its stop takes 18 s (taskkill's
    # 8 s, 5 s for Ollama to exit, a kill and 5 s more), and the stale-API scan is given up 4.9 s past
    # its 30 s. That pass restarts the API and only then finds the window exited, 60.9 s after its
    # start: the window's life ends where its exit was first seen, five seconds in.
    late, scan = 3.0, 34.9

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "taskkill.exe":
            machine.awake(8.0)
        else:
            machine.awake(scan)  # WMI is slow to answer the stale-API scan
            if kwargs.get("timeout"):
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    _windows_with(monkeypatch, run, popen)

    def stubborn_wait(timeout: float | None = None) -> int:
        machine.awake(5.0)
        if not popen.of("ollama")[0][0].kill.called:
            raise subprocess.TimeoutExpired("ollama.exe", timeout or 0)
        return 0

    def healthy(url: str) -> bool:
        if ":11434/" in url:
            ollama = supervisor.processes.get("ollama")
            if ollama is not None and ollama is popen.of("ollama")[0][0] and machine.sleeps >= 3:
                machine.awake(2 * _PROBE)  # the first Ollama hangs: its probe waits out connect and read
                return False
            return ollama is not None
        api = supervisor.processes.get("api")
        return api is not None and api.poll() is None

    def after_sleep(n: int) -> None:
        machine.awake(late)
        windows = [child for child, _ in popen.of("window")]
        if n == 1:
            popen.of("ollama")[0][0].wait.side_effect = stubborn_wait
        if n == 10:
            windows[0].poll.return_value = 3  # the window loses its runtime
        if n == 11 and len(windows) == 2:
            popen.last("api").poll.return_value = 1  # the API crashes under the reopened window...
            exits_at = popen.started_tick[windows[1].pid] + 4.0
            windows[1].poll.side_effect = lambda: 1 if machine.tick >= exits_at else None  # ...which exits 1
        _stop_after(supervisor, n, 20)

    _run(supervisor, machine, monkeypatch, popen, healthy, after_sleep)
    windows = popen.of("window")
    # 4899671 fails this here: it never opens a window that exits 1 again. So does a supervisor that
    # measures the window's life up to the pass that finds it exited: 60.9 s, a forced end.
    assert len(windows) == 3
    reopened, (retried, env) = windows[1][0], windows[2]
    api = popen.last("api")
    assert popen.count("api") == 2
    assert popen.started_tick[api.pid] - popen.started_tick[reopened.pid] > 60.0  # found exited after this
    assert env["VOOL_NATIVE_VOUCHING_PID"] == str(api.pid)  # opened on the restarted API
    log = _log(supervisor)
    assert "the reopened window did not attach to the API (returncode=1)" in log
    assert "ollama retry scheduled reason=unresponsive" in log
    assert "stale api cleanup gave up" in log
    assert "paused" not in log
    assert log.count("window exited") == 2  # the retried window stays open until the stop request
