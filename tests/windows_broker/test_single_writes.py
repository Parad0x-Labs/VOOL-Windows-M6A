"""Single writes through the Windows broker (docs/WINDOWS_BROKER.md sections 4, 6 and 8, Phase C
slice C2).

Once the API has started the broker, workspace.write_file, machine.write_file,
workspace.ensure_directory and machine.ensure_directory on Windows are done by the broker, relative
to the flagged folder it holds, and never by path. The folder's final path is asked first and a
mismatch is refused. `write_file` always carries `expect`: the hash of the bytes VOOL checked, or
`absent`. A broker refusal is a refusal with nothing changed; a reply lost after a change was written
is the client's EffectOutcomeUnknown, never a plain refusal. A process that never started a broker,
and every other OS, keep writing by path as before.

A plain fake stands in for the broker's pipe, so these run on any OS.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from core import runtime_execution_tools as tools
from core import windows_write_gate
from core.windows_broker import session, writes
from core.windows_broker.process import BrokerStatus, FlaggedFolder
from core.windows_broker.protocol import BatchRestored, BrokerRefusal, EffectOutcomeUnknown

FOLDER_ID = "a" * 48


class _Broker:
    """The broker's pipe. Changes are recorded, never made: the tests check nothing lands by path."""

    def __init__(self, folder: Path, *, path: str | None = None, fail: dict[str, Exception] | None = None,
                 created: list[str] | None = None) -> None:
        self.folder = folder
        self.path = str(folder) if path is None else path
        self.fail = fail or {}
        self.created = created
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def connect(self) -> None:
        if "connect" in self.fail:
            raise self.fail["connect"]

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        self.requests.append((operation, arguments))
        if operation in self.fail:
            raise self.fail[operation]
        if operation == "flagged_path":
            return {"ok": True, "path": self.path}
        if operation == "open_root":
            return {"ok": True, "root_id": "r1"}
        if operation == "ensure_directory":
            created = [arguments["rel"]] if self.created is None else self.created
            return {"ok": True, "batch_id": "b1", "results": [{"op": operation, "created": created}], "cleanup_left": []}
        if operation == "write_file":
            sha = hashlib.sha256(base64.b64decode(arguments["data"])).hexdigest()
            return {"ok": True, "batch_id": "b2", "results": [{"op": operation, "path": arguments["rel"],
                                                              "file_id": "f" * 32, "sha256": sha}], "cleanup_left": []}
        raise AssertionError(operation)

    def close(self) -> None:
        self.closed = True

    def ops(self) -> list[str]:
        return [operation for operation, _ in self.requests]

    def last(self, operation: str) -> dict[str, Any]:
        return [arguments for name, arguments in self.requests if name == operation][-1]


@pytest.fixture
def flagged(tmp_path: Path) -> Path:
    folder = tmp_path / "flagged"
    folder.mkdir()
    return folder.resolve()


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    monkeypatch.setattr(tools.policy_engine, "get",
                        lambda key, default=None: True if key == "filesystem.allow_write_workspace" else default)
    return monkeypatch


def _wire(monkeypatch: pytest.MonkeyPatch, broker: _Broker, *folders: FlaggedFolder) -> None:
    class _Host:
        secret = b"s" * 32

    host = _Host()
    host.status = BrokerStatus(True, "", r"\\.\pipe\vool-broker-test", 4242,  # type: ignore[attr-defined]
                               folders or (FlaggedFolder(FOLDER_ID, str(broker.folder)),))
    session.register(host)  # type: ignore[arg-type]
    monkeypatch.setattr(session, "_client_for", lambda status, secret: broker)


def _write(flagged: Path, path: str, content: str, **arguments: Any) -> tools.RuntimeExecutionResult:
    return tools._write_file({"path": path, "content": content, **arguments}, workspace_root=flagged, session_id="s1")


def _sent(broker: _Broker) -> bytes:
    return base64.b64decode(broker.last("write_file")["data"])


def test_a_new_workspace_file_is_written_by_the_broker_with_expect_absent(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged)
    _wire(windows, broker)
    result = _write(flagged, "notes/a.txt", "one\ntwo\n")
    assert result.ok and result.status == "executed", result.response_text
    assert broker.ops() == ["flagged_path", "open_root", "ensure_directory", "write_file"]
    assert broker.last("ensure_directory") == {"root_id": "r1", "rel": "notes"}
    sent = broker.last("write_file")
    assert sent["rel"] == "notes\\a.txt" and sent["expect"] == "absent" and sent["root_id"] == "r1"
    assert _sent(broker) == b"one\ntwo\n"
    assert not (flagged / "notes").exists()  # nothing by path
    assert broker.closed


def test_an_existing_workspace_file_is_written_with_the_hash_vool_read(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    (flagged / "a.txt").write_bytes(b"old\r\n")
    broker = _Broker(flagged)
    _wire(windows, broker)
    result = _write(flagged, "a.txt", "new\n")
    assert result.ok and result.details["action"] == "updated"
    assert broker.ops() == ["flagged_path", "open_root", "write_file"]
    assert broker.last("write_file")["expect"] == "sha256:" + hashlib.sha256(b"old\r\n").hexdigest()
    assert (flagged / "a.txt").read_bytes() == b"old\r\n"


def test_a_reviewed_workspace_write_carries_the_reviewed_hash(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    (flagged / "a.txt").write_bytes(b"reviewed")
    prior = hashlib.sha256(b"reviewed").hexdigest()
    broker = _Broker(flagged)
    _wire(windows, broker)
    target = flagged / "a.txt"
    result = tools._write_file({"path": "a.txt", "content": "x"}, workspace_root=flagged, session_id="s1",
                               reviewed_destination={"target": str(target), "kind": "file", "prior_sha256": prior})
    assert result.ok, result.response_text
    assert broker.last("write_file")["expect"] == "sha256:" + prior


@pytest.mark.parametrize("code", ["changed_since_check", "read_only", "reparse_point", "journal_pending"])
def test_a_broker_refusal_is_a_refusal_with_nothing_changed(windows: pytest.MonkeyPatch, flagged: Path, code: str) -> None:
    broker = _Broker(flagged, fail={"write_file": BrokerRefusal(code, "no")})
    _wire(windows, broker)
    result = _write(flagged, "a.txt", "x")
    assert not result.ok and result.status == windows_write_gate.STATUS
    assert result.details["broker_code"] == code and result.details["executed"] is False
    assert "nothing was changed" in result.response_text.lower()
    assert not (flagged / "a.txt").exists()


def test_a_batch_the_broker_put_back_is_a_refusal(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, fail={"write_file": BatchRestored("io_error", "disk", {"batch_id": "b", "restored": True})})
    _wire(windows, broker)
    result = _write(flagged, "a.txt", "x")
    assert not result.ok and result.details["broker_code"] == "io_error"


def test_folders_made_before_a_refused_write_are_named(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, fail={"write_file": BrokerRefusal("read_only", "no")})
    _wire(windows, broker)
    result = _write(flagged, "new/a.txt", "x")
    assert not result.ok and result.details["created_folders"] == ["new"]
    assert "nothing was changed" not in result.response_text.lower() and "new" in result.response_text


def test_a_reply_lost_after_the_write_is_outcome_unknown_not_a_refusal(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, fail={"write_file": EffectOutcomeUnknown("broker response lost")})
    _wire(windows, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _write(flagged, "a.txt", "x")


def test_a_reply_that_does_not_match_the_bytes_sent_is_outcome_unknown(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged)
    original = broker.request

    def wrong_hash(operation: str, **arguments: Any) -> dict[str, Any]:
        reply = original(operation, **arguments)
        if operation == "write_file":
            reply["results"][0]["sha256"] = "0" * 64
        return reply

    broker.request = wrong_hash  # type: ignore[method-assign]
    _wire(windows, broker)
    with pytest.raises(EffectOutcomeUnknown):
        _write(flagged, "a.txt", "x")


@pytest.mark.parametrize("step", ["connect", "flagged_path", "open_root"])
@pytest.mark.parametrize("error", [ConnectionError("gone"), EffectOutcomeUnknown("lost"), BrokerRefusal("unknown_root", "x")])
def test_a_broker_that_cannot_be_asked_before_the_change_is_a_refusal(windows: pytest.MonkeyPatch, flagged: Path,
                                                                      step: str, error: Exception) -> None:
    broker = _Broker(flagged, fail={step: error})
    _wire(windows, broker)
    result = _write(flagged, "a.txt", "x")
    assert not result.ok and result.status == windows_write_gate.STATUS
    assert "write_file" not in broker.ops() and broker.closed


def test_a_flagged_folder_now_at_another_path_is_refused(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, path=str(flagged.parent / "elsewhere"))
    _wire(windows, broker)
    result = _write(flagged, "a.txt", "x")
    assert not result.ok and result.details["broker_code"] == "flagged_moved"
    assert broker.ops() == ["flagged_path"]


def test_a_broker_that_is_not_running_is_a_refusal(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    class _Host:
        secret = b"s" * 32
        status = BrokerStatus(False, "stopped")

    session.register(_Host())  # type: ignore[arg-type]
    result = _write(flagged, "a.txt", "x")
    assert not result.ok and result.details["broker_code"] == "broker_unavailable"
    assert not (flagged / "a.txt").exists()


def test_a_target_in_no_folder_the_broker_holds_is_refused(windows: pytest.MonkeyPatch, flagged: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    broker = _Broker(flagged)
    _wire(windows, broker)
    result = _write(other.resolve(), "a.txt", "x")
    assert not result.ok and result.details["broker_code"] == "outside_flagged"
    assert broker.requests == []


def test_the_deepest_flagged_folder_holding_the_target_is_used(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    inner = flagged / "inner"
    inner.mkdir()
    broker = _Broker(inner)
    _wire(windows, broker, FlaggedFolder("o" * 48, str(flagged)), FlaggedFolder(FOLDER_ID, str(inner)))
    result = _write(flagged, "inner/a.txt", "x")
    assert result.ok, result.response_text
    assert broker.last("flagged_path") == {"flagged_id": FOLDER_ID}
    assert broker.last("open_root") == {"flagged_id": FOLDER_ID, "rel": ""}
    assert broker.last("write_file")["rel"] == "a.txt"


def test_ensure_directory_goes_through_the_broker(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, created=["x", "x\\y"])
    _wire(windows, broker)
    result = tools._ensure_directory({"path": "x/y"}, workspace_root=flagged)
    assert result.ok and result.status == "executed" and result.details["already_present"] is False
    assert broker.last("ensure_directory") == {"root_id": "r1", "rel": "x\\y"}
    assert not (flagged / "x").exists()


def test_ensure_directory_that_created_nothing_reports_it_was_there(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    (flagged / "x").mkdir()
    broker = _Broker(flagged, created=[])
    _wire(windows, broker)
    result = tools._ensure_directory({"path": "x"}, workspace_root=flagged)
    assert result.ok and result.status == "already_exists"


def test_ensure_directory_refused_by_the_broker(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, fail={"ensure_directory": BrokerRefusal("reparse_point", "link")})
    _wire(windows, broker)
    result = tools._ensure_directory({"path": "x"}, workspace_root=flagged)
    assert not result.ok and result.details["broker_code"] == "reparse_point"


@pytest.fixture
def machine(windows: pytest.MonkeyPatch, flagged: Path) -> pytest.MonkeyPatch:
    """The machine lane, placed in the flagged folder (its own safe-folder rules are tested elsewhere)."""
    windows.setattr(tools, "_resolve_machine_directory", lambda raw, write=False: flagged / str(raw))
    windows.setattr(tools.ExecutionGate, "evaluate_machine_effect", staticmethod(lambda **kw: {"decision": "authorized"}))
    return windows


def test_machine_write_file_goes_through_the_broker_with_text_mode_line_ends(machine: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged)
    _wire(machine, broker)
    result = tools._write_machine_file({"path": "d/m.txt", "content": "a\nb\n"})
    assert result.ok and result.status == "executed", result.response_text
    # Exactly what the by-path writer (Path.write_text, text mode) puts on disk on Windows.
    assert _sent(broker) == b"a\r\nb\r\n"
    assert broker.last("write_file")["expect"] == "absent"
    assert not (flagged / "d").exists()


def test_machine_write_file_over_a_file_carries_its_hash(machine: pytest.MonkeyPatch, flagged: Path) -> None:
    (flagged / "m.txt").write_bytes(b"before")
    broker = _Broker(flagged)
    _wire(machine, broker)
    result = tools._write_machine_file({"path": "m.txt", "content": "after"})
    assert result.ok and result.status == "updated"
    assert broker.last("write_file")["expect"] == "sha256:" + hashlib.sha256(b"before").hexdigest()


def test_machine_write_file_refused_by_the_broker(machine: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged, fail={"write_file": BrokerRefusal("changed_since_check", "changed")})
    _wire(machine, broker)
    result = tools._write_machine_file({"path": "m.txt", "content": "x"})
    assert not result.ok and result.status == windows_write_gate.STATUS
    assert result.details["broker_code"] == "changed_since_check"


def test_machine_ensure_directory_goes_through_the_broker(machine: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged)
    _wire(machine, broker)
    result = tools._ensure_machine_directory({"path": "new"})
    assert result.ok and result.status == "executed"
    assert broker.last("ensure_directory")["rel"] == "new"
    assert not (flagged / "new").exists()


def test_without_a_started_broker_writes_stay_by_path(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    result = _write(flagged, "a.txt", "x")
    assert result.ok and (flagged / "a.txt").read_text(encoding="utf-8") == "x"


def test_off_windows_writes_stay_by_path_even_with_a_host(windows: pytest.MonkeyPatch, flagged: Path) -> None:
    broker = _Broker(flagged)
    _wire(windows, broker)
    windows.setattr(windows_write_gate, "_IS_WINDOWS", False)
    result = _write(flagged, "a.txt", "x")
    assert result.ok and (flagged / "a.txt").read_text(encoding="utf-8") == "x"
    assert broker.requests == []


def test_expect_values() -> None:
    assert writes.expect_for(None) == "absent"
    assert writes.expect_for(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert writes.expect_for_reviewed("") == "absent"
    assert writes.expect_for_reviewed("AB" * 32) == "sha256:" + "ab" * 32
