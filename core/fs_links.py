"""One definition of "this directory entry is a link", correct on Windows as well as POSIX.

`stat.S_ISLNK`, `os.path.islink` and `Path.is_symlink` see only symbolic links. On Windows a
directory junction (created by any user, no privilege needed) redirects a path just like a
symlink, yet all three report False, so a reviewed destination containing one was written
through to a directory nobody reviewed (reproduced on Windows 11, 2026-10-18).

A Windows reparse point counts as a link when its tag carries the name-surrogate bit
(0x20000000): the entry stands for another named object. That covers junctions and volume mount
points, symbolic links, WSL (LX) symlinks and WCI links. Reparse points without it (OneDrive /
cloud-files placeholders, dedup) are the user's ordinary files and folders and are not refused
here. This is link recognition only: it is not a confinement guarantee (a check-then-use path
race remains until the handle-based Windows filesystem authority lands). POSIX behaviour is
exactly `S_ISLNK`.
"""
from __future__ import annotations

import os
import stat

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # directory junction / volume mount point
IO_REPARSE_TAG_SYMLINK = 0xA000000C
_NAME_SURROGATE_BIT = 0x20000000


def is_link_stat(entry: os.stat_result) -> bool:
    """True when an `os.lstat` result describes a link (symlink, or a Windows name surrogate)."""
    if stat.S_ISLNK(entry.st_mode):
        return True
    return bool(getattr(entry, "st_reparse_tag", 0) & _NAME_SURROGATE_BIT)


def is_link(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` itself (not its target) is a link. False when it does not exist."""
    try:
        return is_link_stat(os.lstat(path))
    except FileNotFoundError:
        return False


__all__ = ["IO_REPARSE_TAG_MOUNT_POINT", "IO_REPARSE_TAG_SYMLINK", "is_link", "is_link_stat"]
