"""workspace.rollback_last_change through the Windows broker as one batch (docs/WINDOWS_BROKER.md
sections 6 and 8, Phase C slice C3).

Once the API has started the broker, the last tracked change is put back by one `apply_batch`: a
file the change edited gets its earlier text back (`expect` = the hash of the bytes on disk now,
which the rollback has just checked are the ones the change left), and a file the change created is
deleted (set aside in the broker's journal). Nothing is written or deleted by path. A refused or
restored batch leaves the change tracked and reports the broker's code; a batch the broker could not
put back is the client's BatchNotRestored. Without a started broker, the rollback writes by path as
before.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from core import runtime_execution_tools as tools
from core import windows_write_gate
from core.execution.artifacts import record_workspace_mutation
from core.windows_broker import session
from core.windows_broker.protocol import BatchNotRestored, BatchRestored, BrokerRefusal, EffectOutcomeUnknown

from .test_single_writes import _Broker, _wire


class _BatchBroker(_Broker):
    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation != "apply_batch":
            return super().request(operation, **arguments)
        self.requests.append((operation, arguments))
        if operation in self.fail:
            raise self.fail[operation]
        results = []
        for op in arguments["ops"]:
            if op["op"] == "delete":
                assert set(op) == {"op", "rel"}
                results.append({"op": "delete", "path": op["rel"]})
            else:
                sha = hashlib.sha256(base64.b64decode(op["data"])).hexdigest()
                results.append({"op": "write_file", "path": op["rel"], "file_id": "f" * 32, "sha256": sha})
        return {"ok": True, "batch_id": "b4", "results": results, "cleanup_left": []}


@pytest.fixture
def flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "flagged"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"after\n")
    (folder / "made.txt").write_bytes(b"new\n")
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    root = folder.resolve()
    record_workspace_mutation(session_id="rb1", workspace_root=root, intent="workspace.apply_unified_diff", changes=[
        {"path": "a.txt", "action": "updated", "existed_before": True, "existed_after": True,
         "before_text": "before\n", "after_text": "after\n", "before_mode": None},
        {"path": "made.txt", "action": "created", "existed_before": False, "existed_after": True,
         "before_text": "", "after_text": "new\n", "before_mode": None},
    ])
    return root


def _rollback(flagged: Path) -> tools.RuntimeExecutionResult:
    return tools._rollback_last_change({}, workspace_root=flagged, session_id="rb1")


def test_a_rollback_is_one_batch_restoring_and_deleting_by_handle(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _BatchBroker(flagged)
    _wire(monkeypatch, broker)
    result = _rollback(flagged)
    assert result.ok and result.status == "executed", result.response_text
    assert result.details["restored_paths"] == ["a.txt"] and result.details["removed_paths"] == ["made.txt"]
    assert broker.ops() == ["flagged_path", "open_root", "apply_batch"]
    ops = broker.last("apply_batch")["ops"]
    assert ops == [{"op": "delete", "rel": "made.txt"},
                   {"op": "write_file", "rel": "a.txt", "data": base64.b64encode(b"before\n").decode(),
                    "expect": "sha256:" + hashlib.sha256(b"after\n").hexdigest()}]
    assert (flagged / "a.txt").read_bytes() == b"after\n" and (flagged / "made.txt").exists()  # nothing by path
    assert _rollback(flagged).status == "no_tracked_change"  # the change is marked rolled back


@pytest.mark.parametrize("error", [BrokerRefusal("changed_since_check", "changed"),
                                   BatchRestored("io_error", "disk", {"batch_id": "b", "ran": 1, "restored": True})])
def test_a_refused_rollback_writes_nothing_and_stays_tracked(flagged: Path, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    broker = _BatchBroker(flagged, fail={"apply_batch": error})
    _wire(monkeypatch, broker)
    result = _rollback(flagged)
    assert not result.ok and result.status == windows_write_gate.STATUS
    assert result.details["broker_code"] == error.code  # type: ignore[attr-defined]
    assert result.details["restored_paths"] == [] and result.details["removed_paths"] == []
    broker.fail.clear()
    assert _rollback(flagged).ok  # still tracked, so it can be tried again


def test_a_rollback_the_broker_could_not_put_back_is_outcome_unknown(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = BatchNotRestored("restore_incomplete", "left", "io_error", {"batch_id": "b", "ran": 1, "restored": False, "left": ["a.txt"]})
    broker = _BatchBroker(flagged, fail={"apply_batch": error})
    _wire(monkeypatch, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _rollback(flagged)


def test_a_file_changed_since_is_still_a_conflict_and_nothing_is_sent(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (flagged / "a.txt").write_bytes(b"someone else\n")
    broker = _BatchBroker(flagged)
    _wire(monkeypatch, broker)
    result = _rollback(flagged)
    assert result.status == "stale_revert_conflict"
    assert broker.requests == []


def test_without_a_started_broker_the_rollback_is_by_path(flagged: Path) -> None:
    result = _rollback(flagged)
    assert result.ok, result.response_text
    assert (flagged / "a.txt").read_bytes() == b"before\n" and not (flagged / "made.txt").exists()
