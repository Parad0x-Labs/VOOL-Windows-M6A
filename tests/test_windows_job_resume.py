"""``core.windows_job.resume_suspended`` against real Windows processes (the supervisor tests fake it)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from core.windows_job import CREATE_SUSPENDED, resume_suspended

pytestmark = pytest.mark.skipif(os.name != "nt", reason="CREATE_SUSPENDED and ResumeThread exist only on Windows")


def test_a_suspended_child_runs_only_once_resumed() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "print('ran')"],
        stdout=subprocess.PIPE,
        creationflags=CREATE_SUSPENDED | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1)  # suspended: it has not run
        resume_suspended(child.pid)
        out, _ = child.communicate(timeout=30)
        assert child.returncode == 0
        assert out.strip() == b"ran"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_a_thread_that_is_not_suspended_exactly_once_is_refused() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        with pytest.raises(OSError):
            resume_suspended(child.pid)  # running, not suspended: nothing to resume
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_pid_with_no_threads_is_refused() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    with pytest.raises(OSError):
        resume_suspended(child.pid)
