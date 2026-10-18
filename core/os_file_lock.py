"""Exclusive advisory lock on an open file descriptor, on POSIX and Windows alike.

POSIX uses ``flock``. Windows has no ``fcntl``; modules that imported it at top level could
not even be imported there, so every caller of them failed on Windows. Windows instead locks
one byte far past any content (the same offset ``core.memory.revocation`` uses), so the lock
never blocks a reader of the file's actual bytes.
"""
from __future__ import annotations

import errno
import os
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

_WINDOWS_LOCK_OFFSET = 0x7FFFFFFE
_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


def try_lock_exclusive(fd: int) -> None:
    """Take the exclusive lock now, or raise ``OSError`` when another holder has it."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def lock_exclusive(fd: int) -> None:
    """Block until the exclusive lock is held."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while True:
        try:
            try_lock_exclusive(fd)
            return
        except OSError as exc:
            # Only contention is worth waiting out (measured: EACCES on Windows 11). Any other
            # failure is permanent and surfaces instead of hanging the caller forever.
            if exc.errno not in _CONTENTION_ERRNOS:
                raise
            time.sleep(0.02)


def unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


__all__ = ["lock_exclusive", "try_lock_exclusive", "unlock"]
