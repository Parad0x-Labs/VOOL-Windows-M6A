"""Windows' default text mode for ``os.open``, applied on any host so a POSIX run catches what only Windows corrupts.

On Windows ``os.open`` goes through the C runtime, which opens in text mode unless ``os.O_BINARY`` is passed: every
``\\n`` written through ``os.write`` lands on disk as ``\\r\\n``. Native Windows runs keep the real behavior. Elsewhere,
``os.O_BINARY`` gets the Windows value and a descriptor opened for writing without it translates the same way.
"""
from __future__ import annotations

import os

WINDOWS = os.name == "nt"
O_BINARY = 0x8000


def apply(monkeypatch) -> None:
    if WINDOWS:
        return
    real_open, real_write, real_close = os.open, os.write, os.close
    text: set[int] = set()

    def open_(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags & ~O_BINARY, mode, dir_fd=dir_fd)
        if flags & os.O_ACCMODE != os.O_RDONLY and not flags & O_BINARY:
            text.add(fd)
        else:
            text.discard(fd)
        return fd

    def write(fd, data):
        if fd in text:
            real_write(fd, bytes(data).replace(b"\n", b"\r\n"))
            return len(data)
        return real_write(fd, data)

    def close(fd):
        text.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "O_BINARY", O_BINARY, raising=False)
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "close", close)
