"""Windows' fsync rule, applied on any host so a POSIX run catches what only Windows refuses.

On Windows, ``os.fsync`` needs a handle opened for writing: a read-only one raises EBADF, and a directory cannot be
opened with ``os.open`` at all. Native Windows runs keep the real behavior. Elsewhere, fsync on any read-only
descriptor raises EBADF; a directory descriptor is always read-only, so syncing a directory fails too.
"""
from __future__ import annotations

import errno
import os

WINDOWS = os.name == "nt"


def apply(monkeypatch) -> None:
    if WINDOWS:
        return
    import fcntl

    real_fsync = os.fsync

    def fsync(fd):
        fd = fd if isinstance(fd, int) else fd.fileno()
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
