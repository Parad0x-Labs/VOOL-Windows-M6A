"""Windows refuses a write it will never run BEFORE anyone is asked to approve it.

The Windows write gate (`core.windows_write_gate`) used to be applied only at dispatch, after the
permission decision and after the A6 reservation. A write outside the flagged disposable
workspaces therefore produced an approval card for a write that could never run (Manual), or an
A6 row marked dispatched for a call that was refused (Auto). These drive the real executor
(`execute_tool_intent`) and the authorized execution boundary the fast paths call, with spies that
call through to the real permission and A6 functions.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig
from core.runtime_continuity import compute_logical_effect_id, get_unresolved_effect


def _spy(monkeypatch, name: str) -> list[dict]:
    calls: list[dict] = []
    real = getattr(executor, name)

    def recording(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, name, recording)
    return calls


@pytest.fixture
def spies(monkeypatch) -> dict[str, list[dict]]:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    return {name: _spy(monkeypatch, name) for name in ("decide_tool_call", "reserve_logical_effect", "mark_effect_dispatched")}


def _execute(intent: str, arguments: dict, source_context: dict):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"gate-{uuid.uuid4().hex}",
        session_id=f"gate-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


_WRITE = {"path": "a.txt", "content": "x"}
_GATED_CALLS = {
    "workspace.write_file": dict(_WRITE),
    "code.task.step": {"task_id": "task-1", "intent": "workspace.write_file", "arguments": dict(_WRITE)},
    "repo.step": {"repo_session_id": "repo-1", "intent": "workspace.write_file", "arguments": dict(_WRITE)},
}


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("intent", sorted(_GATED_CALLS))
def test_a_write_outside_the_flagged_folders_is_refused_before_approval_and_a6(
    tmp_path: Path, monkeypatch, spies, intent: str, mode: str
) -> None:
    flagged, workspace = tmp_path / "scratch", tmp_path / "real-project"
    flagged.mkdir()
    workspace.mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(flagged))
    arguments = _GATED_CALLS[intent]

    execution = _execute(intent, arguments, {"workspace": str(workspace), "operating_mode": mode})

    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    row = get_unresolved_effect(compute_logical_effect_id(intent=intent, arguments=arguments))
    assert row is None, {key: row.get(key) for key in ("tool_name", "state", "dispatched_at")}
    assert spies["reserve_logical_effect"] == [] and spies["mark_effect_dispatched"] == []
    assert spies["decide_tool_call"] == [], "no permission decision (no approval card) for a write Windows will refuse"
    assert execution.ok is False and execution.details.get("executed") is False
    assert not (workspace / "a.txt").exists()


def test_an_operator_move_outside_the_flagged_folders_is_refused_before_approval(
    tmp_path: Path, monkeypatch, spies
) -> None:
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    source, destination = tmp_path / "a.txt", tmp_path / "b.txt"
    source.write_text("x", encoding="utf-8")
    arguments = {"source_path": str(source), "destination_path": str(destination)}

    execution = _execute("operator.move_path", arguments, {"workspace": str(tmp_path), "operating_mode": "manual"})

    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    assert spies["decide_tool_call"] == [] and spies["reserve_logical_effect"] == []
    assert get_unresolved_effect(compute_logical_effect_id(intent="operator.move_path", arguments=arguments)) is None
    assert source.exists() and not destination.exists()


_INSIDE_CALLS = {
    "workspace.write_file": lambda root: dict(_WRITE),
    "code.task.step": lambda root: _GATED_CALLS["code.task.step"],
    # A model's `file_path` is bound to `path` exactly as the dispatcher binds it; read raw, the
    # early gate would refuse this call as naming no path.
    "machine.write_file": lambda root: {"file_path": str(root / "notes" / "b.txt"), "content": "x"},
}


@pytest.mark.parametrize("intent", sorted(_INSIDE_CALLS))
def test_inside_a_flagged_workspace_the_write_still_reaches_approval(
    tmp_path: Path, monkeypatch, spies, intent: str
) -> None:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))

    execution = _execute(intent, _INSIDE_CALLS[intent](workspace), {"workspace": str(workspace), "operating_mode": "manual"})

    assert execution.status == "pending_approval", (execution.status, execution.response_text)
    assert len(spies["decide_tool_call"]) == 1
    assert spies["reserve_logical_effect"] == []
    assert not (workspace / "a.txt").exists()


@pytest.mark.parametrize("flag_workspace", [False, True])
def test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision(
    tmp_path: Path, monkeypatch, flag_workspace: bool
) -> None:
    """The machine fast paths and the Blackbox rollback hand no decision to the authorized
    execution boundary, which takes one itself; a refused write must not reach it."""
    from core import authorized_tool_execution as boundary

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    decisions: list[dict] = []
    real = boundary.authorize_runtime_tool
    monkeypatch.setattr(boundary, "authorize_runtime_tool", lambda *a, **k: decisions.append(k) or real(*a, **k))
    flagged, workspace = tmp_path / "scratch", tmp_path / "real-project"
    flagged.mkdir()
    workspace.mkdir()
    monkeypatch.setenv(gate.ENV_VAR, str(workspace if flag_workspace else flagged))
    target = workspace / "notes.txt"

    result = boundary.execute_authorized_runtime_tool(
        "machine.write_file", {"path": str(target), "content": "x"},
        source_context={"workspace": str(workspace), "operating_mode": "manual"},
    )

    if flag_workspace:
        assert result.status == "pending_approval" and len(decisions) == 1, (result.status, result.response_text)
    else:
        assert result.status == gate.STATUS and decisions == [], (result.status, result.response_text)
    assert not target.exists()


def test_a_council_seat_is_gated_on_the_workspace_it_is_re_rooted_to(tmp_path: Path, monkeypatch, spies) -> None:
    """The dispatcher re-roots a granted seat onto its own disposable workspace, so the seat's write
    lands there; gated early on the operator's checkout it would be refused for the wrong root."""
    from core.council import containment

    checkout = tmp_path / "operator-checkout"
    checkout.mkdir()
    run_id = f"gate-{uuid.uuid4().hex[:12]}"
    containment.register_run(run_id, seat_ids=["s1"], grants={"s1": ("workspace_write",)})
    try:
        jail = containment.seat_workspace(run_id, "s1")
        monkeypatch.setenv(gate.ENV_VAR, str(jail))
        context = {
            "workspace": str(checkout),
            "runtime_session_id": containment.seat_session_id_for(run_id, "s1"),
            "operating_mode": "auto",
        }

        execution = _execute("workspace.write_file", dict(_WRITE), context)

        assert execution.ok is True, (execution.status, execution.response_text)
        assert (jail / "a.txt").read_text(encoding="utf-8") == "x"
        assert not (checkout / "a.txt").exists()
    finally:
        containment.release_run(run_id)
