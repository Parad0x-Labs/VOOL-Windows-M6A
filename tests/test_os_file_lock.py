"""core.os_file_lock excludes a second process on POSIX and Windows alike.

The modules that used to import fcntl directly (email drafts, the credential state lock)
could not be imported on Windows at all; they now take this lock, so it must really exclude.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import os_file_lock

REPO = Path(__file__).resolve().parent.parent

_HOLDER = """
import os, sys, time
sys.path.insert(0, {repo!r})
from core import os_file_lock
fd = os.open({path!r}, os.O_CREAT | os.O_RDWR)
os_file_lock.lock_exclusive(fd)
print("HELD", flush=True)
sys.stdin.readline()
os_file_lock.unlock(fd)
os.close(fd)
print("RELEASED", flush=True)
"""


def test_a_held_lock_excludes_another_process_until_released(tmp_path: Path) -> None:
    path = tmp_path / "store.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER.format(repo=str(REPO), path=str(path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(OSError):
                os_file_lock.try_lock_exclusive(fd)
            holder.stdin.write("\n")
            holder.stdin.flush()
            assert holder.stdout.readline().strip() == "RELEASED"
            deadline = time.monotonic() + 10
            while True:
                try:
                    os_file_lock.try_lock_exclusive(fd)
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the released lock never became available"
                    time.sleep(0.05)
            os_file_lock.unlock(fd)
        finally:
            os.close(fd)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_the_lock_does_not_block_reading_the_files_bytes(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    path.write_bytes(b"payload")
    fd = os.open(str(path), os.O_RDWR)
    try:
        os_file_lock.lock_exclusive(fd)
        assert path.read_bytes() == b"payload"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0, "locking must not move the caller's file position"
        os_file_lock.unlock(fd)
    finally:
        os.close(fd)


def test_a_permanent_lock_failure_raises_instead_of_waiting_forever(tmp_path: Path) -> None:
    """Only contention is retried; a bad descriptor must surface at once (run in a child with a
    deadline so a regression fails this test instead of hanging the suite)."""
    probe = (
        f"import os, sys; sys.path.insert(0, {str(REPO)!r}); from core import os_file_lock;"
        f"fd = os.open({str(tmp_path / 'x.lock')!r}, os.O_CREAT | os.O_RDWR); os.close(fd);"
        "\ntry:\n    os_file_lock.lock_exclusive(fd)\nexcept OSError:\n    print('RAISED')"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "RAISED", done.stdout + done.stderr


def test_the_former_fcntl_only_modules_import_on_this_platform() -> None:
    import importlib

    for name in ("core.email_drafts", "core.credential_intelligence._state_lock"):
        importlib.import_module(name)
