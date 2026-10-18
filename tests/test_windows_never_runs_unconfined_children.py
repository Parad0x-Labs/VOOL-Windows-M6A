"""Windows never runs a plugin, MCP server or process agent unconfined.

Plan v2 (owner-approved 2026-10-18): Windows has no kernel confinement backend yet, and untrusted
plugins, parsers and agent commands never run bare there, not even with the explicit
`confinement: "heuristic_only"` opt-in that other hosts honour. The Windows decision is driven
on every platform through the module switches; POSIX behaviour is pinned unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import plugin_executor


def _no_kernel_backend(monkeypatch) -> None:
    monkeypatch.setattr(plugin_executor, "_kernel_confinement_prefix", lambda *a, **k: None)


def test_windows_refuses_heuristic_only(tmp_path: Path, monkeypatch) -> None:
    _no_kernel_backend(monkeypatch)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_BARE", True)
    with pytest.raises(plugin_executor.ConfinementUnavailableError, match="Windows"):
        plugin_executor.confined_argv(["tool"], writable_roots=(tmp_path,), mode="heuristic_only")


def test_windows_still_refuses_the_default_mode(tmp_path: Path, monkeypatch) -> None:
    _no_kernel_backend(monkeypatch)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_BARE", True)
    with pytest.raises(plugin_executor.ConfinementUnavailableError):
        plugin_executor.confined_argv(["tool"], writable_roots=(tmp_path,))


def test_other_hosts_keep_the_explicit_heuristic_only_opt_in(tmp_path: Path, monkeypatch) -> None:
    _no_kernel_backend(monkeypatch)
    monkeypatch.setattr(plugin_executor, "_WINDOWS_REFUSES_BARE", False)
    argv, label = plugin_executor.confined_argv(["tool"], writable_roots=(tmp_path,), mode="heuristic_only")
    assert argv == ["tool"] and label == "heuristic_only"


def test_windows_refuses_to_start_a_process_agent(monkeypatch) -> None:
    from core.agent_team import coordinator

    monkeypatch.setattr(coordinator, "_WINDOWS_REFUSES_PROCESS_AGENTS", True)
    finished: list[tuple[str, str, str]] = []
    team = coordinator.TeamCoordinator.__new__(coordinator.TeamCoordinator)
    team._finish = lambda agent_id, forced_state="", note="": finished.append((agent_id, forced_state, note))
    launched: list[object] = []
    monkeypatch.setattr(coordinator.subprocess, "Popen", lambda *a, **k: launched.append(a))

    team._launch_process({"agent_id": "a1"}, object(), Path("."))

    assert launched == [], "no child process may be started"
    assert finished and finished[0][:2] == ("a1", "refused") and "Windows" in finished[0][2]
