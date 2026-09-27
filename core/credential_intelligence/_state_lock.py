"""A cross-process read-modify-write lock for the credential-intelligence state files.

``os.replace`` makes each WRITE atomic, but every read-modify-write over these JSON files
(save one provider's row, drop another's, invalidate a third's) is NOT: two processes or
threads that each read the old file and replace their own version lose whichever row they did
not hold (measured by the 2026-09-15 review probe: two concurrent saves for different
providers, one survivor). An in-process ``threading.Lock`` covers one process only — the
daemon and a CLI surface are separate processes sharing the same data dir.

This module delegates to ``core.cross_process_lock.PublicationLock``, the shared OS-backed lock
authority (``flock`` on POSIX, ``msvcrt.locking`` on Windows), on a dedicated lock file next to the
state file (``<name>.lock``), across the whole read-modify-write. It composes with the existing
atomic-replace writer (the replace stays the crash-atomicity mechanism; the lock is the concurrency
mechanism). Same-thread re-entry is NOT supported — callers must not take the lock and then, inside
it, drive another door that takes the same lock (the test rigs inject concurrent state changes
between lock sections, never inside one).
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from core.cross_process_lock import PublicationLock


@contextlib.contextmanager
def state_lock(state_path: Path) -> Iterator[None]:
    """Hold the exclusive lock guarding read-modify-write access to ``state_path``."""
    path = Path(state_path)
    lock_path = path.with_name(path.name + ".lock")
    with PublicationLock(lock_path):
        yield


def credential_state_lock():
    """The transaction lock guarding the credential binding index -- the ONE authority every
    credential writer already holds. Readers that must observe a multi-slot fact atomically
    (the custom provider's base URL and its key) take the SAME lock, so a writer's
    endpoint+key+index commit is indivisible to them too."""
    from core.credential_intelligence.binding import index_path

    return state_lock(index_path())


__all__ = ["credential_state_lock", "state_lock"]
