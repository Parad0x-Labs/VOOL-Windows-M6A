"""Carry 12: on Windows the tour copy names only what Windows serves.

Steve, 15 Nov: the tour faults and the registry told the owner to replay the tour from
"Settings → Show me around", and the pact refusal said "press 'Show me around' first". The Windows
Settings page has no such entry, so the advice pointed at nothing. On Windows the tour is started
only by POST /api/onboarding/pact/begin, which a person in chat cannot call, so the lines they read
say plainly that the tour isn't available in the Windows app in this version; only the
developer-facing command description names the endpoint. Off Windows the Settings entry exists, so
the Mac copy is unchanged.
"""
from __future__ import annotations

import json

import pytest

from core import first_run_pact, windows_write_gate
from core.command_registry.registry import CommandRegistry
from core.first_run_pact import PactFault
from tests.first_run_pact_rig import pact_rig  # noqa: F401

BEGIN = "POST /api/onboarding/pact/begin"


def _not_applicable_home() -> None:
    from core.runtime_paths import active_data_dir

    pact_file = active_data_dir() / "first_run_pact_state.json"
    pact_file.parent.mkdir(parents=True, exist_ok=True)
    pact_file.write_text(json.dumps({
        "schema_name": "vool.first_run_pact", "schema_version": 1, "state": "not_applicable",
        "revision": 0, "welcome_hidden": False, "seed_reason": "existing_user",
        "steps": {}, "created_at": "2026-09-05T00:00:00Z", "updated_at": "2026-09-05T00:00:00Z",
    }))


def _refusal() -> str:
    with pytest.raises(PactFault) as excinfo:
        first_run_pact.set_boundary("local_only_composite", True)
    assert excinfo.value.code == "not_applicable_readonly"
    return str(excinfo.value)


def _first_run_specs(monkeypatch, windows: bool):
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", windows)
    from core.command_registry.groups import first_run

    reg = CommandRegistry()
    first_run.register(reg)
    return reg


def _spec(reg, command_id: str):
    return next((spec for spec in reg.commands() if spec.command_id == command_id), None)


def _all_copy(reg) -> str:
    parts = []
    for spec in reg.commands():
        parts.append(spec.description)
        for binding in spec.fault_bindings:
            parts.extend(binding.remediation)
    return "\n".join(parts)


def test_on_windows_the_pact_refusal_names_the_begin_endpoint(pact_rig, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    _not_applicable_home()

    text = _refusal()

    assert "Show me around" not in text
    # The person reading this uses chat: no HTTP call they cannot make, just the plain truth.
    assert "/api/" not in text and "POST" not in text
    assert "isn't available in the Windows app" in text


def test_off_windows_the_pact_refusal_keeps_the_settings_entry(pact_rig, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    _not_applicable_home()

    assert "press 'Show me around' first" in _refusal()


def test_on_windows_no_first_run_command_points_at_show_me_around(monkeypatch) -> None:
    reg = _first_run_specs(monkeypatch, windows=True)
    copy = _all_copy(reg)

    assert "Show me around" not in copy
    begin = _spec(reg, "first_run.pact.begin")
    assert begin is not None and BEGIN in begin.description
    remediation = [
        line
        for binding in begin.fault_bindings
        if binding.when == "not_applicable_readonly"
        for line in binding.remediation
    ]
    assert remediation, remediation
    for line in remediation:
        assert "/api/" not in line and "POST" not in line, line
        assert "isn't available in the Windows app" in line, line


def test_off_windows_the_first_run_commands_keep_the_settings_entry(monkeypatch) -> None:
    reg = _first_run_specs(monkeypatch, windows=False)

    begin = _spec(reg, "first_run.pact.begin")
    assert begin is not None
    assert begin.description == "Start or replay the tour (Settings → Show me around)"
