"""`pid_gone` says "gone" only on proof; every doubt keeps a process alive.

The effect-budget reconciler frees a dead holder's reservations on this answer, so a live process
must never read as gone. Runs on every OS: on Windows it drives the OpenProcess/WaitForSingleObject
path, on POSIX the os.kill(pid, 0) path.
"""

from __future__ import annotations

import os
import subprocess
import sys

from core.process_liveness import pid_gone


def test_this_process_is_not_gone() -> None:
    assert pid_gone(os.getpid()) is False


def test_non_positive_pids_are_never_gone() -> None:
    assert pid_gone(0) is False
    assert pid_gone(-1) is False


def test_a_running_child_is_not_gone() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert pid_gone(child.pid) is False
    finally:
        child.kill()
        child.wait(timeout=30)


def test_an_exited_child_is_gone_even_while_its_handle_is_held() -> None:
    # On Windows the Popen object still holds the process handle, so the kernel keeps the
    # process object: the proof is that it is signaled. On POSIX wait() reaped it.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    assert pid_gone(child.pid) is True
