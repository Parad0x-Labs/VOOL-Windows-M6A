"""One list of flagged folders for the gate and the broker (docs/WINDOWS_BROKER.md section 4,
Phase C slice C1).

Once the API has started (or tried to start) the broker, the Windows gate takes its flagged
folders from the broker's start line instead of re-reading VOOL_WINDOWS_DISPOSABLE_WORKSPACES, and
asks the broker for each folder's current final path (flagged_path, answered from the handle the
broker holds) before it accepts a target in it. A folder whose path is no longer the one the
broker reported, a broker that is not running, and a broker that cannot be asked all leave no
folder marked, so every gated call is refused. A process that never started a broker (tests,
tools run outside the API) keeps reading the variable as before.

Plain fakes stand in for the broker process and its pipe, so these run on any OS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import windows_write_gate
from core.windows_broker import session
from core.windows_broker.process import BrokerHost, BrokerStatus, FlaggedFolder
from core.windows_broker.protocol import BrokerRefusal


class _Process:
    def __init__(self, line: bytes) -> None:
        self.pid = 4242
        self.stdin = _Sink()
        self.stdout = _Lines([line])
        self.stderr = None
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class _Sink:
    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Lines:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def close(self) -> None:
        pass


class _Job:
    def assign(self, pid: int) -> None:
        pass

    def close(self) -> None:
        pass


def _started(tmp_path: Path, flagged: Any) -> BrokerHost:
    line = json.dumps({"pipe": r"\\.\pipe\vool-broker-test", "broker_pid": 4242, "flagged": flagged,
                       "journal": {"available": True}}).encode() + b"\n"
    exe = tmp_path / "VoolBroker.exe"
    exe.write_bytes(b"")
    host = BrokerHost(exe, [], tmp_path / "journal", popen=lambda argv, **options: _Process(line),
                      job_factory=_Job, api_identity=lambda: (1, 2), timeout=5.0)
    host.start()
    return host


class _Client:
    """The broker's pipe: flagged_path answers from `paths` (id -> path, or an exception to raise)."""

    def __init__(self, paths: dict[str, Any], *, connect_error: Exception | None = None) -> None:
        self.paths = paths
        self.connect_error = connect_error
        self.asked: list[str] = []
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        assert operation == "flagged_path"
        self.asked.append(arguments["flagged_id"])
        answer = self.paths[arguments["flagged_id"]]
        if isinstance(answer, Exception):
            raise answer
        return {"ok": True, "path": answer}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    made = {name: tmp_path / name for name in ("from_variable", "from_broker", "other")}
    for folder in made.values():
        folder.mkdir()
    return made


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, folders: dict[str, Path]) -> pytest.MonkeyPatch:
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(windows_write_gate.ENV_VAR, str(folders["from_variable"]))
    monkeypatch.setattr(session, "_host", None)
    return monkeypatch


def _wire(monkeypatch: pytest.MonkeyPatch, status: BrokerStatus, client: _Client | None = None) -> None:
    class _Host:
        secret = b"s" * 32

    host = _Host()
    host.status = status  # type: ignore[attr-defined]
    session.register(host)  # type: ignore[arg-type]
    if client is not None:
        monkeypatch.setattr(session, "_client_for", lambda status, secret: client)


def _write(target: Path) -> str | None:
    return windows_write_gate.refusal("workspace.write_file", {"path": "f.txt"}, target)


def _running(*flagged: FlaggedFolder) -> BrokerStatus:
    return BrokerStatus(True, "", r"\\.\pipe\vool-broker-test", 4242, tuple(flagged))


def test_the_host_keeps_the_start_lines_flagged_list(tmp_path: Path) -> None:
    host = _started(tmp_path, [{"id": "a" * 48, "path": r"C:\work\a"}, {"id": "b" * 48, "path": r"C:\work\b"}])
    assert host.status.running
    assert host.status.flagged == (FlaggedFolder("a" * 48, r"C:\work\a"), FlaggedFolder("b" * 48, r"C:\work\b"))


@pytest.mark.parametrize("flagged", [None, "C:\\work", [{"id": "a" * 48}], [{"id": 7, "path": "C:\\work"}],
                                     [{"id": "a" * 48, "path": ""}]])
def test_a_start_line_without_a_well_formed_flagged_list_is_refused(tmp_path: Path, flagged: Any) -> None:
    status = _started(tmp_path, flagged).status
    assert not status.running and status.reason == "bad_start_line"


def test_the_gate_takes_the_brokers_folders_not_the_variable(windows: pytest.MonkeyPatch, folders: dict[str, Path]) -> None:
    client = _Client({"a" * 48: str(folders["from_broker"])})
    _wire(windows, _running(FlaggedFolder("a" * 48, str(folders["from_broker"]))), client)
    assert _write(folders["from_broker"]) is None
    assert _write(folders["from_variable"]) is not None
    assert client.asked and client.closed


def test_a_folder_whose_path_changed_since_start_is_not_marked(windows: pytest.MonkeyPatch, folders: dict[str, Path]) -> None:
    client = _Client({"a" * 48: str(folders["other"])})
    _wire(windows, _running(FlaggedFolder("a" * 48, str(folders["from_broker"]))), client)
    assert _write(folders["from_broker"]) is not None
    assert _write(folders["other"]) is not None


@pytest.mark.parametrize("answer", [BrokerRefusal("unknown_root", "gone"), ConnectionError("broker is unavailable")])
def test_a_folder_the_broker_cannot_vouch_for_is_not_marked(windows: pytest.MonkeyPatch, folders: dict[str, Path],
                                                             answer: Exception) -> None:
    client = _Client({"a" * 48: answer, "b" * 48: str(folders["other"])})
    _wire(windows, _running(FlaggedFolder("a" * 48, str(folders["from_broker"])),
                            FlaggedFolder("b" * 48, str(folders["other"]))), client)
    assert _write(folders["from_broker"]) is not None
    if isinstance(answer, BrokerRefusal):
        assert _write(folders["other"]) is None  # the other folder still answers


def test_a_broker_that_cannot_be_reached_marks_no_folder(windows: pytest.MonkeyPatch, folders: dict[str, Path]) -> None:
    client = _Client({}, connect_error=ConnectionError("broker is unavailable"))
    _wire(windows, _running(FlaggedFolder("a" * 48, str(folders["from_broker"]))), client)
    reason = _write(folders["from_broker"])
    assert reason is not None and "No folder is marked" in reason


@pytest.mark.parametrize("reason", ["not_started", "not_found", "refused:reparse_point", "stopped"])
def test_a_broker_that_is_not_running_marks_no_folder(windows: pytest.MonkeyPatch, folders: dict[str, Path], reason: str) -> None:
    _wire(windows, BrokerStatus(False, reason))
    refused = _write(folders["from_variable"])
    assert refused is not None and "No folder is marked" in refused


def test_without_a_broker_started_the_gate_reads_the_variable(windows: pytest.MonkeyPatch, folders: dict[str, Path]) -> None:
    assert _write(folders["from_variable"]) is None
    assert _write(folders["from_broker"]) is not None


def test_start_for_api_registers_its_host_for_the_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from core.windows_broker import process

    monkeypatch.setattr(session, "_host", None)
    monkeypatch.setattr(process, "_is_windows", lambda: True)
    monkeypatch.setattr(process, "broker_executable", lambda root: None)
    host = process.start_for_api(tmp_path, tmp_path)
    assert session.wired() and session._host is host and host.status.reason == "not_found"
