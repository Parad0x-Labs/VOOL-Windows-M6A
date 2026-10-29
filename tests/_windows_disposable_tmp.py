"""Each test's own tmp_path is the disposable workspace it writes into, so on Windows it is marked as one.

On Windows the write gate refuses every user-file write outside the folders listed in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES (core/windows_write_gate.py). The suite never listed any, so tests
written for Mac and Linux, which write only into their own tmp_path, were refused there. Only that one
test's folder is listed: never the home, Documents, TEMP or the checkout. A test that sets or clears the
variable itself still decides for itself, since its own monkeypatch runs after this one.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.windows_write_gate import ENV_VAR


def mark_own_tmp_path_disposable(monkeypatch, tmp_path: Path, *, windows: bool) -> None:
    if windows:
        monkeypatch.setenv(ENV_VAR, str(tmp_path))


def running_on_windows() -> bool:
    return os.name == "nt"
