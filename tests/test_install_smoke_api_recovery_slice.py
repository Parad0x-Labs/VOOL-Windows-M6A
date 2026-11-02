"""Installed slice (Step 4.2, stop/restart/supervisor recovery): a killed API comes back on its own.

The install smoke kills the API listener it has already proved is VOOL's own python under the install
folder, checks that pid is gone (the negative control: if the old pid still served, recovery would be
unproven), then waits for /healthz and requires a NEW listener that is again VOOL's python under the
install folder. It runs after the launch checks and before the restart phase, which then proves Stop
still leaves nothing behind. The PowerShell runs on Windows only; this pins its shape.
"""
from __future__ import annotations

import re
from pathlib import Path

SMOKE = Path(__file__).resolve().parents[1] / "tools" / "windows_install_smoke.ps1"


def _section() -> str:
    text = SMOKE.read_text(encoding="utf-8")
    start = text.index("# --- installed slice: API recovery")
    return text[start:text.index("# --- restart", start)]


def test_the_recovery_slice_runs_after_launch_and_before_the_restart_phase() -> None:
    text = SMOKE.read_text(encoding="utf-8")

    assert text.index('Require "launch" "window_host_alive_after_settle"') < text.index("# --- installed slice: API recovery") < text.index("# --- restart")


def test_it_kills_only_the_listener_it_proved_is_vools_python() -> None:
    section = _section()

    assert "Stop-Process -Id $killedApi -Force" in section
    assert re.search(r"\$killedApi = \$listener\.ProcessId", section)
    assert section.count("Stop-Process") == 1


def test_its_negative_control_requires_the_killed_pid_to_be_gone() -> None:
    section = _section()

    assert 'Require "slices" "api_recovery_control_killed_pid_gone"' in section
    assert section.index("api_recovery_control_killed_pid_gone") < section.index("api_recovered_healthz_200")


def test_recovery_needs_a_new_listener_that_is_again_vools_python() -> None:
    section = _section()

    assert 'Require "slices" "api_recovered_healthz_200"' in section
    new_listener = section[section.index('"api_recovered_as_new_vools_python"') - 400:]
    assert "-ne $killedApi" in new_listener
    assert "$InstallRoot" in new_listener and "vool_api_server.py" in new_listener
