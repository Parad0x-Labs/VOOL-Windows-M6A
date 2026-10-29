"""Windows: retry a replace or unlink that a sharing violation refuses for a moment.

On Windows `os.replace` and `os.unlink` fail with PermissionError (WinError 5 or 32) while another
process holds the target open without delete sharing: a reader of the same store, an antivirus scan or
the search indexer. Those handles close within moments, so the call is retried for a bounded time
(about 0.65 s) and the last refusal is raised when it still fails. Off Windows a PermissionError is a
real refusal and is raised at once. Tests take the Windows path through `_IS_WINDOWS`.
"""
from __future__ import annotations

import os
import time

#: Patched by tests to drive the Windows rule on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"
#: The pauses between attempts. One more attempt follows the last pause.
_DELAYS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1, 0.1, 0.1)


def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """`os.replace`, retried on Windows while a sharing violation refuses it."""
    _retried(os.replace, src, dst)


def unlink(path: str | os.PathLike[str], *, missing_ok: bool = False) -> None:
    """`os.unlink`, retried on Windows while a sharing violation refuses it."""
    try:
        _retried(os.unlink, path)
    except FileNotFoundError:
        if not missing_ok:
            raise


def _retried(call, *args) -> None:
    if _IS_WINDOWS:
        for delay in _DELAYS:
            try:
                call(*args)
                return
            except PermissionError:
                time.sleep(delay)
    call(*args)
