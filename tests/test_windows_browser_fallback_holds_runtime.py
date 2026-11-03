"""Windows without WebView2: the browser fallback keeps the runtime it opened a window onto.

vool_window.main() starts and owns the runtime, then, when no WebView2 runtime is installed, opens
the UI through vool-open.ps1 (an Edge app window). It used to return 0 at once, and its finally
block shut the owned runtime down, so the Edge window opened onto a runtime that was already gone.
The host now holds the runtime while the opener (which waits for its Edge window) runs, and
releases it when that window closes, as closing the native window does. These tests drive the hold
directly with fakes, so they run on every host.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _nw():
    spec = importlib.util.spec_from_file_location("nw_fallback_hold", REPO / "installer" / "bundle" / "vool_window.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOpener:
    """The opener process: running for `runs` polls, then exits with `code`."""

    def __init__(self, runs: int, code: int = 0) -> None:
        self.runs, self.code, self.returncode, self.stopped = runs, code, None, False

    def poll(self):
        if self.returncode is None:
            if self.runs <= 0:
                self.returncode = self.code
            self.runs -= 1
        return self.returncode


class FakeSupervisor:
    owns_runtime = True

    def __init__(self, dies_after: int | None = None) -> None:
        self.dies_after, self.checks, self.shutdowns = dies_after, 0, 0

    def assert_alive(self) -> None:
        self.checks += 1
        if self.dies_after is not None and self.checks > self.dies_after:
            raise RuntimeError("owned runtime child exited (returncode=1)")

    def shutdown(self) -> None:
        self.shutdowns += 1


def _hold(nw, supervisor, opener, monkeypatch):
    stopped: list[object] = []
    monkeypatch.setattr(nw, "_stop_opener", lambda proc: stopped.append(proc))
    monkeypatch.setattr(nw.time, "sleep", lambda _s: None)
    return nw._hold_runtime_for_browser(supervisor, opener), stopped


def test_the_runtime_is_held_until_the_edge_window_closes(monkeypatch):
    nw = _nw()
    supervisor, opener = FakeSupervisor(), FakeOpener(runs=5, code=0)
    rc, stopped = _hold(nw, supervisor, opener, monkeypatch)
    assert rc == 0
    assert supervisor.checks >= 5, "the runtime was released before its window closed"
    assert supervisor.shutdowns == 0, "the hold itself never tears the runtime down; main's finally does"
    assert stopped == []


def test_a_lost_runtime_ends_the_hold_with_exit_3_and_closes_the_window(monkeypatch):
    nw = _nw()
    supervisor, opener = FakeSupervisor(dies_after=2), FakeOpener(runs=100, code=0)
    rc, stopped = _hold(nw, supervisor, opener, monkeypatch)
    assert rc == 3
    assert stopped == [opener], "the Edge window must not outlive its runtime"


def test_a_default_browser_window_keeps_the_runtime_until_it_exits(monkeypatch):
    nw = _nw()
    supervisor = FakeSupervisor(dies_after=7)
    opener = FakeOpener(runs=1, code=nw._OPENER_UNWATCHED)
    rc, stopped = _hold(nw, supervisor, opener, monkeypatch)
    assert rc == 3
    assert supervisor.checks == 8, "an unwatchable browser window must not release the runtime early"
    assert stopped == [], "there is no opener left to stop"


def test_main_holds_the_runtime_on_the_webview2_absent_path(monkeypatch):
    nw = _nw()
    calls: list[str] = []
    supervisor = FakeSupervisor()
    opener = FakeOpener(runs=0)
    monkeypatch.setattr(nw, "_fallback_to_edge", lambda: calls.append("fallback") or opener)
    monkeypatch.setattr(
        nw, "_hold_runtime_for_browser", lambda sup, proc: calls.append("hold") or (sup is supervisor and proc is opener and 0)
    )
    assert nw._open_windows_browser_fallback(supervisor) == 0
    assert calls == ["fallback", "hold"]


def test_no_opener_means_nothing_to_hold(monkeypatch):
    nw = _nw()
    monkeypatch.setattr(nw, "_fallback_to_edge", lambda: None)
    monkeypatch.setattr(nw, "_hold_runtime_for_browser", lambda *a: (_ for _ in ()).throw(AssertionError("held")))
    assert nw._open_windows_browser_fallback(FakeSupervisor()) == 0


def test_the_opener_waits_for_its_edge_window_and_reports_an_unwatchable_one():
    script = (REPO / "installer" / "bundle" / "vool-open.ps1").read_text(encoding="utf-8")
    edge_launch = script[script.index("Start-Process -FilePath $edge"):]
    assert "-Wait" in edge_launch.split("} else {")[0], "the opener must wait for its Edge window"
    assert "exit 10" in script.split("} else {", 1)[1], "a default-browser window is reported as unwatchable"
