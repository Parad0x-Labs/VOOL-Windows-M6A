"""A stalled plugin-folder probe is taken down with everything it started.

A probe interpreter can have a child of its own: on Windows a venv's python.exe is a launcher
that runs the real interpreter as a child, and that child is the one blocked on the folder.
Killing only the probe's pid left that interpreter running, holding the folder open and the
probe's output pipes, so the two threads that read those pipes on Windows never ended either.
The stalled probe here starts such a child itself, so the case is the same on every OS.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from core import plugin_catalog as pc
from core.process_liveness import pid_alive

_PROBE_WITH_A_CHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_a_stalled_probe_and_the_interpreter_it_started_are_both_gone(tmp_path: Path, monkeypatch) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    child_pid_file = tmp_path / "child.pid"
    monkeypatch.setattr(
        pc,
        "_plugin_probe_command",
        lambda _dir: [sys.executable, "-I", "-S", "-c", _PROBE_WITH_A_CHILD, str(child_pid_file)],
    )
    threads_before = threading.active_count()

    result = pc.probe_plugin_storage(plugins, budget_s=2.0)

    assert result["state"] == pc.STORAGE_STALLED, result
    assert child_pid_file.exists(), "the probe never started its child"
    child = int(child_pid_file.read_text(encoding="utf-8"))
    try:
        assert _wait_until(lambda: not pid_alive(child)), "the probe's child still holds the folder"
        assert not pid_alive(result["probe_pid"])
        assert threading.active_count() == threads_before, "a thread still reads the dead probe's pipes"
    finally:
        if pid_alive(child):  # never leave the sleeper behind for the rest of the suite
            import os
            import signal

            os.kill(child, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
