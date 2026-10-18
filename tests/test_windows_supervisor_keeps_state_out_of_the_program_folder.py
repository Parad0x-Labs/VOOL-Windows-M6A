"""A directly-run bundle supervisor keeps its state in the user's VOOL home, not the program folder.

The launcher sets VOOL_HOME before starting the supervisor; `bundle_supervisor.py --stop` run by
hand (or by an installer step) did not, so the runtime home fell back to <app>\\app\\.vool_local
and the preflight wrote its record into the program folder. Install smoke run 39099269490 found
two such records there, which the uninstaller deletes along with the program files.
"""
from __future__ import annotations

import sys
from pathlib import Path

from core import runtime_paths
from installer.bundle import bundle_supervisor


def test_a_direct_stop_runs_its_preflight_in_the_native_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VOOL_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    expected = runtime_paths.native_runtime_home()
    assert runtime_paths.active_vool_home() != expected, "precondition: without VOOL_HOME the home is not the native one"

    seen: list[Path] = []
    monkeypatch.setattr("core.unattended_preflight.preflight", lambda surface: seen.append(runtime_paths.active_vool_home()))
    monkeypatch.setattr(sys, "argv", ["bundle_supervisor.py", "--stop"])

    assert bundle_supervisor.main() == 0
    assert seen == [expected]
    assert (expected / "run" / "stop").read_text(encoding="utf-8").strip() == "stop"
