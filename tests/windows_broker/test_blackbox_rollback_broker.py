"""The Blackbox turn rollback (and so code.task.rollback) through the Windows broker
(docs/WINDOWS_BROKER.md sections 6 and 8, Phase C slice C3).

The Blackbox rollback acts step by step and journals each step, so each step becomes one broker
request: a restore is `write_file` with `expect` = the hash of the bytes the rollback has just
checked are on disk (`absent` when none), and a remove (a created file, then each folder the turn
made, or an empty folder the turn made) is `delete`. Nothing is written or removed by path. A
refused step stops the rollback as a failed step does today, and the next attempt resumes. Without
a started broker the rollback is by path as before.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from core import windows_write_gate
from core.mode_permission_policy import (
    PermissionAction,
    grant_internal_authority,
    reset_mode_permission_state,
    set_active_mode,
)
from core.runtime_execution_tools import execute_runtime_tool
from core.windows_broker import session
from core.windows_broker.protocol import BrokerRefusal

from .test_single_writes import _Broker, _wire


class _StepBroker(_Broker):
    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation != "delete":
            return super().request(operation, **arguments)
        self.requests.append((operation, arguments))
        if operation in self.fail:
            raise self.fail[operation]
        return {"ok": True, "batch_id": "b5", "results": [{"op": "delete", "path": arguments["rel"]}], "cleanup_left": []}


@pytest.fixture(autouse=True)
def _permission_state():
    reset_mode_permission_state()
    yield
    reset_mode_permission_state()


@pytest.fixture
def turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A turn recorded by path (no broker yet): one overwrite, one create in a new folder, one new folder."""
    monkeypatch.setenv("VOOL_BLACKBOX_DIR", str(tmp_path / "blackbox-store"))
    from core.blackbox import store as store_module

    store_module.reset_default_store()
    workspace = (tmp_path / "ws")
    workspace.mkdir()
    workspace = workspace.resolve()
    (workspace / "bin.dat").write_bytes(b"\x00raw\xff")
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", False)
    monkeypatch.setattr(session, "_host", None)
    set_active_mode("bb", "auto")
    ctx = {"workspace": str(workspace), "session_id": "bb", "surface": "api", "operating_mode": "auto"}
    from core.effect_gateway import named_background_effect_scope

    with named_background_effect_scope("test.turn", source_context=ctx):
        wrote = execute_runtime_tool("workspace.write_file", {"path": "bin.dat", "content": "text"}, source_context=ctx)
        created = execute_runtime_tool("workspace.write_file", {"path": "made/new.txt", "content": "new"}, source_context=ctx)
        made = execute_runtime_tool("workspace.ensure_directory", {"path": "empty-dir"}, source_context=ctx)
    assert wrote.ok and created.ok and made.ok
    turn_id = str((wrote.details.get("blackbox") or {}).get("turn_id") or "")
    assert turn_id
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    yield workspace, turn_id
    store_module.reset_default_store()


def _rollback(workspace: Path, turn_id: str):
    from core.blackbox.operator import rollback_turn

    token = grant_internal_authority(
        label="broker-test-rollback",
        actions=[PermissionAction.DELETE_FILES, PermissionAction.OVERWRITE_EXISTING_FILES, PermissionAction.MODIFY_FILES],
        intents=["workspace.rollback_last_change"], workspace_root=str(workspace), duration_seconds=300,
    )
    return rollback_turn(turn_id, workspace_root=workspace, session_id="bb", operator="test-operator",
                         source_context={"operating_mode": "auto", "surface": "api"}, authority_token=token)


def test_each_rollback_step_is_one_broker_request(turn: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, turn_id = turn
    broker = _StepBroker(workspace)
    _wire(monkeypatch, broker)
    result = _rollback(workspace, turn_id)
    assert result.ok and result.status == "executed", result.response_text
    writes = [arguments for name, arguments in broker.requests if name == "write_file"]
    assert len(writes) == 1 and writes[0]["rel"] == "bin.dat"
    assert writes[0]["expect"] == "sha256:" + hashlib.sha256(b"text").hexdigest()
    assert base64.b64decode(writes[0]["data"]) == b"\x00raw\xff"
    deletes = [arguments["rel"] for name, arguments in broker.requests if name == "delete"]
    assert sorted(deletes) == ["empty-dir", "made", "made\\new.txt"]
    assert deletes.index("made\\new.txt") < deletes.index("made")
    # Nothing by path: the broker fake records changes and makes none.
    assert (workspace / "bin.dat").read_bytes() == b"text" and (workspace / "made" / "new.txt").exists()
    summary = result.details["blackbox_rollback"]
    assert summary["restored_paths"] == ["bin.dat"]
    assert sorted(summary["removed_paths"]) == ["empty-dir", "made", "made/new.txt"]


def test_a_refused_step_stops_the_rollback_and_a_later_attempt_resumes(turn: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, turn_id = turn
    broker = _StepBroker(workspace, fail={"write_file": BrokerRefusal("changed_since_check", "changed")})
    _wire(monkeypatch, broker)
    result = _rollback(workspace, turn_id)
    assert not result.ok
    assert "changed_since_check" in result.response_text or "changed_since_check" in str(result.details)
    assert (workspace / "bin.dat").read_bytes() == b"text"
    broker.fail.clear()
    again = _rollback(workspace, turn_id)
    assert again.ok, again.response_text


def test_a_folder_the_broker_finds_not_empty_is_left_and_named(turn: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, turn_id = turn
    broker = _StepBroker(workspace)
    original = broker.request

    def made_not_empty(operation: str, **arguments: Any) -> dict[str, Any]:
        if operation == "delete" and arguments["rel"] == "made":
            broker.requests.append((operation, arguments))
            raise BrokerRefusal("not_empty", "made holds a file")
        return original(operation, **arguments)

    broker.request = made_not_empty  # type: ignore[method-assign]
    _wire(monkeypatch, broker)
    result = _rollback(workspace, turn_id)
    assert result.ok, result.response_text
    assert result.details["blackbox_rollback"]["left_nonempty_dirs"] == ["made"]


def test_without_a_started_broker_the_rollback_is_by_path(turn: tuple[Path, str]) -> None:
    workspace, turn_id = turn
    windows_write_gate._IS_WINDOWS = False  # restored by the fixture's monkeypatch
    result = _rollback(workspace, turn_id)
    assert result.ok, result.response_text
    assert (workspace / "bin.dat").read_bytes() == b"\x00raw\xff" and not (workspace / "made").exists()


def test_a_lost_reply_leaves_the_step_open_not_failed(turn: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker reply lost after the restore was sent may have changed the file: the step stays open for
    `recover_open_effects` to reconcile against the disk, and is never journaled as a failed restore."""
    from core.blackbox import store as store_module
    from core.windows_broker.protocol import EffectOutcomeUnknown

    workspace, turn_id = turn
    broker = _StepBroker(workspace, fail={"write_file": EffectOutcomeUnknown("broker response lost")})
    _wire(monkeypatch, broker)
    try:
        result = _rollback(workspace, turn_id)
    except Exception:  # either surfacing is fine; the journal is what is checked
        result = None
    assert result is None or not result.ok
    store = store_module.default_store()
    open_paths = [record.path for record in store.open_effects()]
    assert open_paths == ["bin.dat"], open_paths
    assert "restore_failed" not in str(result.details if result is not None else "")
