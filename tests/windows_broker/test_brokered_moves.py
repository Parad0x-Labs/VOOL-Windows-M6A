"""machine.move_path and operator.move_path through the Windows broker's rename
(docs/WINDOWS_BROKER.md sections 6 and 8, Phase C slice C4).

Once the API has started the broker, a move on Windows is the broker's `rename`: the source is
held and renamed onto the held destination folder, never replacing, and across two flagged folders
the second one is opened as `to_root_id`. Nothing is moved by path. A move across volumes is refused
`cross_volume` (today `shutil.move` copies, so this is the behaviour change section 6 names). A
broker refusal is a refusal with nothing moved; a lost reply is the client's EffectOutcomeUnknown,
never a plain failure. Without a started broker the move is by path as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import local_operator_actions, machine_file_ops, windows_write_gate
from core.operator import handlers as operator_handlers
from core.windows_broker import session
from core.windows_broker.process import FlaggedFolder
from core.windows_broker.protocol import BrokerRefusal, EffectOutcomeUnknown

from .test_single_writes import FOLDER_ID, _Broker, _wire


class _MoveBroker(_Broker):
    def __init__(self, folder: Path, **options: Any) -> None:
        super().__init__(folder, **options)
        self.roots: list[str] = []

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation == "open_root":
            self.requests.append((operation, arguments))
            self.roots.append(arguments["flagged_id"])
            return {"ok": True, "root_id": f"r{len(self.roots)}"}
        if operation == "flagged_path":
            self.requests.append((operation, arguments))
            return {"ok": True, "path": self.paths.get(arguments["flagged_id"], self.path)}
        if operation != "rename":
            return super().request(operation, **arguments)
        self.requests.append((operation, arguments))
        if operation in self.fail:
            raise self.fail[operation]
        return {"ok": True, "batch_id": "b6", "results": [{"op": "rename", "path": arguments["to"], "file_id": "f" * 32}],
                "cleanup_left": []}

    paths: dict[str, str] = {}


@pytest.fixture
def flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "flagged"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"a")
    (folder / "dest").mkdir()
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    return folder.resolve()


def _move(src: Path, dst: Path) -> machine_file_ops.FileOpResult:
    return machine_file_ops.move_path(str(src), str(dst), consent_fn=lambda _text: True, protected=[],
                                      allowed_roots=[src.parent, dst.parent])


def test_a_machine_move_is_the_brokers_rename(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _MoveBroker(flagged)
    _wire(monkeypatch, broker)
    result = _move(flagged / "a.txt", flagged / "dest" / "b.txt")
    assert result.ok and result.status == "moved", result.message
    assert broker.last("rename") == {"root_id": "r1", "rel": "a.txt", "to": "dest\\b.txt", "replace": False}
    assert (flagged / "a.txt").exists() and not (flagged / "dest" / "b.txt").exists()  # nothing by path


def test_a_move_between_two_flagged_folders_names_the_second_root(flagged: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = (tmp_path / "other")
    other.mkdir()
    other = other.resolve()
    broker = _MoveBroker(flagged)
    broker.paths = {FOLDER_ID: str(flagged), "o" * 48: str(other)}
    _wire(monkeypatch, broker, FlaggedFolder(FOLDER_ID, str(flagged)), FlaggedFolder("o" * 48, str(other)))
    result = _move(flagged / "a.txt", other / "a.txt")
    assert result.ok, result.message
    assert broker.roots == [FOLDER_ID, "o" * 48]
    assert broker.last("rename") == {"root_id": "r1", "rel": "a.txt", "to": "a.txt", "replace": False, "to_root_id": "r2"}
    assert [arguments["flagged_id"] for name, arguments in broker.requests if name == "flagged_path"] == [FOLDER_ID, "o" * 48]


@pytest.mark.parametrize("code", ["cross_volume", "destination_exists", "in_use", "reparse_point"])
def test_a_refused_move_moves_nothing(flagged: Path, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    broker = _MoveBroker(flagged, fail={"rename": BrokerRefusal(code, "no")})
    _wire(monkeypatch, broker)
    result = _move(flagged / "a.txt", flagged / "dest" / "b.txt")
    assert not result.ok and result.status == windows_write_gate.STATUS
    assert code in result.message and "nothing was moved" in result.message.lower()


def test_a_lost_reply_is_outcome_unknown_not_a_failure(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _MoveBroker(flagged, fail={"rename": EffectOutcomeUnknown("lost")})
    _wire(monkeypatch, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _move(flagged / "a.txt", flagged / "dest" / "b.txt")


def test_without_a_started_broker_a_machine_move_is_by_path(flagged: Path) -> None:
    result = _move(flagged / "a.txt", flagged / "dest" / "b.txt")
    assert result.ok and (flagged / "dest" / "b.txt").exists()


def _operator_move(flagged: Path, destination_dir: Path) -> Any:
    pending = {"action_id": "act-1", "scope_json": json.dumps({
        "source_path": str(flagged / "a.txt"), "destination_dir": str(destination_dir),
        "destination_path": str(destination_dir / "a.txt")})}

    class _Gate:
        requires_user_approval = False
        mode = "execute"
        reason = ""

    move_fn, ensure_directory_fn, reraise = local_operator_actions._move_functions()
    intent = type("Intent", (), {"action_id": "act-1", "approval_requested": True})()
    return operator_handlers.handle_move_path(
        intent, task_id="t", session_id="s",
        load_pending_action_fn=lambda *a, **kw: pending, parse_move_request_fn=None, validate_move_scope_fn=lambda s, d: "",
        resolved_move_target_fn=None, create_pending_action_fn=None, mark_action_executed_fn=lambda *a, **kw: None,
        evaluate_local_action_fn=lambda *a, **kw: _Gate(), move_fn=move_fn, audit_log_fn=lambda *a, **kw: None,
        ensure_directory_fn=ensure_directory_fn, reraise=reraise,
    )


def test_an_operator_move_makes_its_folder_and_renames_through_the_broker(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _MoveBroker(flagged)
    _wire(monkeypatch, broker)
    result = _operator_move(flagged, flagged / "archive")
    assert result.ok, result.response_text
    assert broker.last("ensure_directory")["rel"] == "archive"
    assert broker.last("rename") == {"root_id": "r2", "rel": "a.txt", "to": "archive\\a.txt", "replace": False}
    assert not (flagged / "archive").exists()


def test_an_operator_move_whose_reply_is_lost_is_outcome_unknown(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _MoveBroker(flagged, fail={"rename": EffectOutcomeUnknown("lost")})
    _wire(monkeypatch, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _operator_move(flagged, flagged / "dest")


def test_an_operator_move_refused_by_the_broker_fails_with_its_code(flagged: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _MoveBroker(flagged, fail={"rename": BrokerRefusal("cross_volume", "other volume")})
    _wire(monkeypatch, broker)
    result = _operator_move(flagged, flagged / "dest")
    assert not result.ok and "cross_volume" in result.response_text
