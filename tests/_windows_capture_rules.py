"""Windows' file API limits for package capture, applied on any host so a POSIX run takes the Windows path.

Windows has no ``os.O_NOFOLLOW`` and no ``dir_fd``: ``os.listdir`` needs a path, ``os.open`` cannot open a
directory, and a read-only file cannot be deleted. Native Windows runs keep the real behavior. Elsewhere, those
limits are enforced, ``core.plugin_package`` is told it runs on Windows, and its no-follow open
(``_open_no_follow``, the Win32 ``CreateFile`` call) is stood in for by the POSIX no-follow open it replaces.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat

WINDOWS = os.name == "nt"


def apply(monkeypatch) -> None:
    if WINDOWS:
        return
    from core import plugin_package

    nofollow, directory_flag = os.O_NOFOLLOW, os.O_DIRECTORY
    real_open, real_stat, real_listdir, real_unlink = os.open, os.stat, os.listdir, os.unlink

    def open_no_follow(path: str, *, directory: bool) -> int:
        return real_open(path, os.O_RDONLY | nofollow | (directory_flag if directory else 0))

    def open_(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable on this platform")
        if stat.S_ISDIR(real_stat(path).st_mode):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return real_open(path, flags, mode)

    def stat_(path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None or isinstance(path, int):
            raise NotImplementedError("dir_fd unavailable on this platform")
        return real_stat(path, follow_symlinks=follow_symlinks)

    def listdir(path="."):
        if isinstance(path, int):
            raise NotImplementedError("listdir by descriptor unavailable on this platform")
        return real_listdir(path)

    def unlink(path, *, dir_fd=None):
        info = real_stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode) and not info.st_mode & stat.S_IWUSR:
            raise PermissionError(errno.EACCES, "Access is denied", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.delattr(os, "O_NOFOLLOW")
    monkeypatch.delattr(os, "O_DIRECTORY")
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "stat", stat_)
    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(shutil, "_use_fd_functions", False)
    monkeypatch.setattr(plugin_package, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(plugin_package, "_open_no_follow", open_no_follow, raising=False)
