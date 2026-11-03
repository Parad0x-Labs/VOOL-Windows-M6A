"""Each test's own tmp_path is the disposable workspace it writes into, so on Windows it is marked as one.

On Windows the write gate refuses every user-file write outside the folders listed in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES (core/windows_write_gate.py). The suite never listed any, so tests
written for Mac and Linux, which write only into their own tmp_path, were refused there. Only that one
test's folder is listed: never the home, Documents, TEMP or the checkout. A test that sets or clears the
variable itself still decides for itself, since its own monkeypatch runs after this one.

unittest rigs build their workspace with tempfile instead of tmp_path. On Windows each test also gets
its own folder under pytest's basetemp (`tempfile_root`), tempfile puts its folders there for that
test, and that folder is listed too. The system TEMP is still never listed.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.windows_write_gate import ENV_VAR

#: the prefix root conftest.py gives the session home it makes with mkdtemp
SESSION_HOME_PREFIX = "vool_pytest_home_"


def mark_own_tmp_path_disposable(
    monkeypatch,
    tmp_path: Path,
    *,
    windows: bool,
    tempfile_root: Path | None = None,
    session_workspace: Path | None = None,
) -> None:
    if not windows:
        return
    listed = [str(tmp_path)]
    if tempfile_root is not None:
        monkeypatch.setattr(tempfile, "tempdir", str(tempfile_root))
        listed.append(str(tempfile_root))
    if session_workspace is not None:
        listed.append(str(session_workspace))
    monkeypatch.setenv(ENV_VAR, os.pathsep.join(listed))


def session_pytest_workspace() -> Path | None:
    """VOOL_HOME/workspace when VOOL_HOME is the suite's own session home (root conftest.py), else None."""
    home = os.environ.get("VOOL_HOME", "")
    if not home or not Path(home).name.startswith(SESSION_HOME_PREFIX):
        return None
    return Path(home) / "workspace"


def running_on_windows() -> bool:
    return os.name == "nt"
