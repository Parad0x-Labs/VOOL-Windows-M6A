"""Windows: what chat says after a write into a marked folder, round 1 of the captest ship loop.

Installed build 828b8bda on a fresh Windows 11 VM (2026-11-09). Writes into marked folders landed
after one approval, with the exact bytes, but:

- R1-2: every such write answered "Created file ..." and then "I cannot verify that file from this
  turn's execution record". The machine writer records the file home-relative
  (`~/VOOL Test/Allowed Ä folder/hello.txt`), the marked-folder lane's read-back reply names it by its
  full path, and the final honesty guard compared the two spellings as text.
- R1-3: "create hello.txt in C:\\Users\\VOOLT\\VOOL Test\\Allowed Ä folder with the text hi" was still
  refused while the reply listed that folder as writable. The input normalizer split `Test\\Allowed` on
  its backslash, so the write-root check read the path from "...\\VOOL Test \\ Allowed Ä folder".
- R1-5: a write that climbed out of a marked folder with `..`, and a move whose destination is outside
  the marked folders, changed nothing, but neither answer was a plain refusal saying why.
- SA1: nothing logged that a write went through VoolBroker.

The Windows decisions are driven on Linux through the gates' `_IS_WINDOWS` flags, as the other Windows
tests do. A drive-letter path is read on a small simulated Windows disk (`_windows_disk`); real NTFS is
not touched here.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import windows_known_folders, windows_path_policy
from core import windows_write_gate as gate
from core.agent_runtime import action_honesty_validator as honesty
from core.agent_runtime import write_root_honesty
from core.input_normalizer import normalize_user_text

SPACED = r"C:\Users\VOOLT\VOOL Test\Allowed Ä folder"


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home with Desktop, Documents and Downloads; the chat's (unmarked) workspace; and a marked
    folder with spaces and a non-ASCII letter INSIDE the home, as `C:\\Users\\VOOLT\\VOOL Test\\...`
    was on the VM."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = tmp_path / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    marked = home / "VOOL Test" / "Allowed Ä folder"
    marked.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    return {"home": home, "workspace": workspace, "marked": marked}


def _ask(folder: Path | str) -> str:
    return f"create hello.txt in {folder} with the text hi"


def _served(machine: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str,
            *, mode: str = "auto") -> str:
    """One turn as the VOOL window sends it: streamed /api/chat, an unbound chat whose workspace is the
    unmarked Desktop. The reply is the text of every streamed frame."""
    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from apps.vool_api_server import _dispatch_post
    from core import runtime_paths
    from core.web.api.runtime import RuntimeServices

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(machine["workspace"]))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    try:
        agent = VoolAgent(backend_name="test-backend", device="write-lane-honesty", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", agent)
        vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
        body = {
            "messages": [{"role": "user", "content": text}],
            "session_id": f"openclaw:{uuid.uuid4().hex[:20]}",
            "stream": True,
            "workspace": str(machine["workspace"]),
            "mode": mode,
        }
        response = _dispatch_post(
            path="/api/chat", body=body, headers={"content-type": "application/json"},
            runtime=RuntimeServices(display_name="VOOL", agent=agent), model_name="vool",
            workspace_root_provider=lambda: str(machine["workspace"]),
        )
        raw = b"".join(response.stream or ())
    finally:
        runtime_paths.configure_runtime_home(None)
    assert response.status == 200
    frames = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return "".join(str((frame.get("message") or {}).get("content") or "") for frame in frames if isinstance(frame, dict))


def _files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


# --- R1-2: a write the lane read back is not called unverifiable ---------------------------------


def test_a_write_into_a_marked_folder_under_the_home_is_answered_from_its_read_back(machine, tmp_path, monkeypatch) -> None:
    reply = _served(machine, tmp_path, monkeypatch, _ask(machine["marked"]))
    landed = machine["marked"] / "hello.txt"
    assert landed.read_bytes() == b"hi"
    assert "cannot verify" not in reply, reply
    assert "read it back" in reply and str(landed) in reply, reply


def _write_receipt(recorded_path: str) -> dict[str, Any]:
    """The receipt the machine fast path leaves for a write the tool ran: its observation names the
    file as `_home_relative_label` does."""
    return {
        "tool_name": "machine.write_file", "mode": "tool_executed", "status": "executed",
        "execution": {"executed": True, "ok": True, "status": "executed"},
        "observation": {"intent": "machine.write_file", "ok": True, "status": "executed", "path": recorded_path},
    }


def _guard(response: str, receipts: list[dict[str, Any]]) -> str:
    out = honesty.enforce_final_action_honesty(
        {"response": response, "mode": "tool_executed"},
        user_input="create hello.txt in the marked folder with the text hi",
        session_id="write-lane-honesty",
        source_context={"tool_receipts": receipts, "workspace": "/elsewhere/workspace"},
    )
    return str(out["response"])


def test_the_guard_matches_a_home_relative_record_to_the_full_path_the_reply_names(machine) -> None:
    landed = machine["marked"] / "hello.txt"
    reply = f"Created `{landed}` and read it back: it holds exactly the text you asked for (1 line)."
    assert _guard(reply, [_write_receipt("~/VOOL Test/Allowed Ä folder/hello.txt")]) == reply


def test_the_guard_still_refuses_a_claim_about_another_file_under_the_home(machine) -> None:
    other = machine["marked"] / "other.txt"
    reply = f"Created `{other}` and read it back: it holds exactly the text you asked for (1 line)."
    assert "cannot verify" in _guard(reply, [_write_receipt("~/VOOL Test/Allowed Ä folder/hello.txt")])


def test_the_guard_still_refuses_a_write_claim_with_no_write_recorded(machine) -> None:
    landed = machine["marked"] / "hello.txt"
    reply = f"Created `{landed}` and read it back: it holds exactly the text you asked for (1 line)."
    assert "cannot verify" in _guard(reply, [])


# --- R1-3: an unquoted spaced Windows path survives the input normalizer --------------------------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (rf"create hello.txt in {SPACED} with the text hi", SPACED),
        (rf"create {SPACED}\hello.txt with the text hi", SPACED + r"\hello.txt"),
        (rf"create {SPACED}\..\escape.txt with the text hi", SPACED + r"\..\escape.txt"),
        (rf"move {SPACED}\hello.txt to C:\Users\VOOLT\Documents", SPACED + r"\hello.txt to C:\Users\VOOLT\Documents"),
    ],
)
def test_the_normalizer_keeps_a_spaced_windows_path_as_typed(text: str, kept: str) -> None:
    assert kept in normalize_user_text(text).normalized_text


class _WindowsDisk:
    """`os` as `write_root_honesty` uses it, answering from a fixed set of Windows folders. Win32 drops
    trailing spaces and dots from a path's last name, and so does this."""

    def __init__(self, folders: set[str]) -> None:
        self.folders = {self._key(folder) for folder in folders}
        self.name = "nt"
        self.path = SimpleNamespace(isdir=self.isdir, exists=self.isdir, normcase=ntpath.normcase)

    @staticmethod
    def _key(path: str) -> str:
        normal = ntpath.normpath(path)
        head, tail = ntpath.split(normal)
        return ntpath.normcase(ntpath.join(head, tail.rstrip(" .")) if tail else normal)

    def isdir(self, path: str) -> bool:
        return self._key(path) in self.folders

    def scandir(self, parent: str):
        wanted = self._key(parent)
        names = [ntpath.basename(folder) for folder in self.folders if ntpath.dirname(folder) == wanted]

        class _Entries(list):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return _Entries(SimpleNamespace(name=name) for name in names)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.fixture
def windows_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    disk = _WindowsDisk({r"C:\Users", r"C:\Users\VOOLT", r"C:\Users\VOOLT\VOOL Test", SPACED})
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_path_separators", lambda: "\\/")
    monkeypatch.setattr(write_root_honesty, "os", disk)
    # GetDriveTypeW answers DRIVE_FIXED (3) for C: on the VM: a drive Windows calls local.
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: False if drive.upper() == "C:" else None)


@pytest.mark.parametrize(
    ("text", "destination"),
    [
        (rf"create hello.txt in {SPACED} with the text hi", SPACED),
        (rf"create {SPACED}\..\escape.txt with the text hi", SPACED + r"\..\escape.txt"),
    ],
)
def test_on_windows_the_write_check_reads_the_spaced_path_whole_from_the_normalized_turn(
    windows_disk, text: str, destination: str
) -> None:
    # The front door's write-root check reads the normalizer's text (`effective_input`).
    effective = normalize_user_text(text).normalized_text
    assert write_root_honesty.write_destinations(effective) == [destination]


@pytest.mark.parametrize(("kind", "remote"), [(3, False), (2, False), (6, False), (4, True), (0, None), (1, None), (5, None)])
def test_a_fixed_drive_is_one_windows_calls_local(monkeypatch: pytest.MonkeyPatch, kind: int, remote: bool | None) -> None:
    """C: on the VM is DRIVE_FIXED (3): a drive the spaced-path walk may look at. Only a network drive
    (4) is remote; one Windows cannot name is treated as remote by every caller."""
    import ctypes

    asked: list[str] = []

    def get_drive_type(root: str) -> int:
        asked.append(root)
        return kind

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(GetDriveTypeW=get_drive_type)), raising=False)
    assert windows_known_folders._drive_is_remote("C:") is remote
    assert asked == ["C:\\"]


# --- R1-5: a climb out of a marked folder, and a move out of one, are plain refusals ---------------


def test_a_trailing_climb_is_kept_as_part_of_the_path(machine) -> None:
    marked = machine["marked"]
    text = f"create escape.txt in {marked}{os.sep}.. with the text hi"
    assert write_root_honesty.write_destinations(text) == [f"{marked}{os.sep}.."]
    assert write_root_honesty.write_destinations(f"create escape.txt in {marked}{os.sep}... with the text hi") == [f"{marked}{os.sep}.."]


def test_the_marked_lane_never_writes_a_climb_into_the_marked_folder(machine) -> None:
    from core.agent_runtime.fast_paths_machine import _extract_marked_folder_write_target

    assert _extract_marked_folder_write_target(f"create escape.txt in {machine['marked']}{os.sep}.. with the text hi") is None


@pytest.mark.parametrize("form", ["{marked}/../escape.txt", "escape.txt in {marked}/..", "escape.txt in {marked}/../.."])
def test_a_write_that_climbs_out_of_a_marked_folder_is_refused_and_says_why(machine, tmp_path, monkeypatch, form: str) -> None:
    named = form.format(marked=machine["marked"])
    reply = _served(machine, tmp_path, monkeypatch, f"create {named} with the text hi")
    assert "I can't write to" in reply and "`..`" in reply and "Nothing was written" in reply, reply
    assert "cannot verify" not in reply
    assert _files(machine["home"]) == []


def test_the_climb_reply_names_where_the_path_leads(machine) -> None:
    climbing = f"{machine['marked']}{os.sep}..{os.sep}escape.txt"
    reply = write_root_honesty.unreachable_write_response(climbing, source_context={"workspace": str(machine["workspace"])})
    assert f"`{machine['marked'].parent / 'escape.txt'}`" in reply, reply
    assert "outside every folder you marked" in reply and "Nothing was written" in reply


def _move(target: str | None, destination: str | None, raw_text: str):
    from core.operator.models import OperatorActionIntent

    return OperatorActionIntent(kind="move_path", target_path=target, destination_path=destination, raw_text=raw_text)


def test_an_unquoted_move_out_of_the_marked_folders_is_a_plain_refusal(machine) -> None:
    from core.local_operator_actions import windows_write_refusal

    source = machine["marked"] / "hello.txt"
    source.write_bytes(b"hi")
    outside = machine["home"] / "Documents"
    text = f"move {source} to {outside}"
    # The Windows parser reads the unquoted source greedily, through " to ...", and no destination.
    reason = windows_write_refusal(_move(text[len("move "):], None, text))
    assert reason is not None
    assert f"`{outside}`" in reason and "outside the folders you marked" in reason and "Nothing was moved" in reason, reason
    assert "quoted source path" not in reason
    assert source.read_bytes() == b"hi" and _files(outside) == []


def test_a_quoted_move_out_of_the_marked_folders_names_the_destination(machine) -> None:
    from core.local_operator_actions import windows_write_refusal

    source = machine["marked"] / "hello.txt"
    source.write_bytes(b"hi")
    outside = machine["home"] / "Documents"
    reason = windows_write_refusal(_move(str(source), str(outside), f'move "{source}" to "{outside}"'))
    assert reason is not None and f"`{outside}`" in reason and "Nothing was moved" in reason, reason


def test_a_move_inside_the_marked_folders_is_not_refused_by_this(machine) -> None:
    from core.local_operator_actions import windows_write_refusal

    source = machine["marked"] / "hello.txt"
    source.write_bytes(b"hi")
    (machine["marked"] / "sub").mkdir()
    text = f"move {source} to {machine['marked'] / 'sub'}"
    assert windows_write_refusal(_move(text[len("move "):], None, text)) is None


def test_off_windows_an_unquoted_move_is_left_to_the_handler(machine, monkeypatch) -> None:
    from core.local_operator_actions import windows_write_refusal

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    text = f"move {machine['marked'] / 'hello.txt'} to {machine['home'] / 'Documents'}"
    assert windows_write_refusal(_move(text[len("move "):], None, text)) is None


# --- SA1: a write routed through the broker is logged ---------------------------------------------


@pytest.fixture
def broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.windows_broker import session
    from tests.windows_broker.test_single_writes import _Broker, _wire

    folder = (tmp_path / "flagged").resolve()
    folder.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(session, "_host", None)
    fake = _Broker(folder)
    _wire(monkeypatch, fake)
    return SimpleNamespace(folder=folder, fake=fake)


def _broker_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == "core.windows_broker.writes" and record.levelno == logging.INFO]


def test_a_broker_routed_write_logs_one_line_with_path_op_and_result(broker, caplog) -> None:
    from core.windows_broker import writes

    target = broker.folder / "notes" / "hello.txt"
    caplog.set_level(logging.INFO, logger="core.windows_broker.writes")
    writes.write_file(target, b"the private words", "absent")
    lines = _broker_lines(caplog)
    assert len(lines) == 1, lines
    assert "write_file" in lines[0] and str(target) in lines[0] and "done" in lines[0], lines
    assert "private" not in lines[0]


def test_a_refused_broker_write_logs_the_refusal(broker, caplog, monkeypatch) -> None:
    from core.windows_broker import writes
    from core.windows_broker.protocol import BrokerRefusal

    broker.fake.fail = {"write_file": BrokerRefusal("read_only", "no")}
    caplog.set_level(logging.INFO, logger="core.windows_broker.writes")
    with pytest.raises(writes.Refused):
        writes.write_file(broker.folder / "hello.txt", b"x", "absent")
    lines = _broker_lines(caplog)
    assert len(lines) == 1 and "refused (read_only)" in lines[0], lines


def test_the_machine_writer_routed_through_the_broker_is_logged(broker, caplog, monkeypatch) -> None:
    from core import runtime_execution_tools as tools

    # The machine lane placed in the flagged folder, as tests/windows_broker/test_single_writes.py does.
    monkeypatch.setattr(tools, "_resolve_machine_directory", lambda raw, write=False: broker.folder / str(raw))
    monkeypatch.setattr(tools.ExecutionGate, "evaluate_machine_effect", staticmethod(lambda **kw: {"decision": "authorized"}))
    target = broker.folder / "hello.txt"
    caplog.set_level(logging.INFO, logger="core.windows_broker.writes")
    result = tools._write_machine_file({"path": "hello.txt", "content": "hi"})
    assert result.ok, result.response_text
    assert not target.exists()  # the fake broker records the change; nothing is written by path
    lines = _broker_lines(caplog)
    assert any("write_file" in line and str(target) in line and "done" in line for line in lines), lines


# --- Windows CI at 396445b1: the served marked-folder test failed only on real Windows ------------
#
# tests/test_windows_marked_folder_writes.py::test_the_served_chat_asks_to_approve_the_write_in_the_marked_folder
# was answered "outside every folder" on the Windows runner and passed on Linux. Each candidate cause
# gets its own check below, so one Windows run tells them apart; the layer-by-layer test runs on the
# real disk of whatever OS runs it, with no fakes, and its failure message lists every layer's answer.


def _layers(folder: Path, workspace: Path) -> dict[str, Any]:
    """What each layer makes of the CI test's request, for the assertion messages."""
    import unicodedata

    text = _ask(folder)
    effective = normalize_user_text(text).normalized_text
    drive = ntpath.splitdrive(str(folder))[0]
    return {
        "os.name": os.name,
        "typed": text,
        "normalized": effective,
        "folder_is_NFC": unicodedata.is_normalized("NFC", str(folder)),
        "drive": drive,
        "drive_is_remote": windows_known_folders._drive_is_remote(drive) if drive and os.name == "nt" else "n/a",
        "normcase(folder)": os.path.normcase(str(folder)),
        "realpath(folder)": os.path.realpath(str(folder)),
        "flagged_roots": gate.flagged_roots(),
        "scandir(parent)": sorted(entry.name for entry in os.scandir(folder.parent)),
        "destinations(typed)": write_root_honesty.write_destinations(text),
        "destinations(normalized)": write_root_honesty.write_destinations(effective),
        "unreachable(normalized)": write_root_honesty.unreachable_write_target(
            effective, source_context={"workspace": str(workspace)}),
    }


def test_layer_by_layer_the_served_request_names_the_marked_folder_whole(tmp_path: Path, monkeypatch) -> None:
    """The CI test's own layout: the marked folder under the test's temp folder, outside HOME."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    marked = tmp_path / "profile" / "VOOL Test" / "Allowed Ä folder"
    marked.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    facts = _layers(marked, workspace)
    report = "\n".join(f"  {key}: {value!r}" for key, value in facts.items())
    # 1. The front door's text: the normalizer must keep the path as typed (the `\` split).
    assert str(marked) in facts["normalized"], f"normalizer changed the path\n{report}"
    # 2. The raw text: the write-root reader must take the folder whole.
    assert facts["destinations(typed)"] == [str(marked)], f"raw text read short\n{report}"
    # 3. The normalized text, as the front door reads it.
    assert facts["destinations(normalized)"] == [str(marked)], f"normalized text read short\n{report}"
    # 4. And it is reachable, so the front door does not refuse it.
    assert facts["unreachable(normalized)"] == "", f"refused as unreachable\n{report}"


def _marked_on_windows(monkeypatch, marked: str) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_path_separators", lambda: "\\/")
    monkeypatch.setenv(gate.ENV_VAR, marked)
    monkeypatch.setattr(gate, "flagged_roots", lambda environ=None: [ntpath.normcase(marked)])
    monkeypatch.setattr(gate, "_store_start_folders", lambda: [])


@pytest.mark.parametrize("drive_answer", [None, True], ids=["drive-type-unknown", "drive-said-remote"])
def test_a_marked_folder_typed_whole_is_read_whole_whatever_the_drive_type_says(monkeypatch, drive_answer) -> None:
    """Candidate 1: GetDriveTypeW outside {2, 3, 6}, or ctypes failing (None). The disk walk is then
    skipped, as 828b8bda wants; a folder typed exactly as marked is still read whole, with no lookup."""
    _marked_on_windows(monkeypatch, SPACED)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: drive_answer)
    monkeypatch.setattr(write_root_honesty, "os", _WindowsDisk(set()))  # the disk says nothing
    assert write_root_honesty.write_destinations(rf"create hello.txt in {SPACED} with the text hi") == [SPACED]
    assert write_root_honesty.write_destinations(rf"create {SPACED}\hello.txt with the text hi") == [SPACED + r"\hello.txt"]


@pytest.mark.parametrize(
    "typed",
    [SPACED.upper(), SPACED.replace("\\", "/"), SPACED.lower()],
    ids=["other-case", "forward-slashes", "lower-case-a-umlaut"],
)
def test_a_marked_folder_is_matched_without_case_and_with_either_separator(monkeypatch, typed: str) -> None:
    """Candidates 2 and 3: case folding (`Ä`/`ä`) and `\\` against `/`, with the disk walk unable to help."""
    _marked_on_windows(monkeypatch, SPACED)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: None)
    monkeypatch.setattr(write_root_honesty, "os", _WindowsDisk(set()))
    assert write_root_honesty.write_destinations(f"create hello.txt in {typed} with the text hi") == [typed]


def test_a_longer_name_that_starts_like_a_marked_folder_is_not_taken_for_it(monkeypatch) -> None:
    _marked_on_windows(monkeypatch, SPACED)
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda drive: None)
    monkeypatch.setattr(write_root_honesty, "os", _WindowsDisk(set()))
    for other in (SPACED + "s", SPACED + ".backup", SPACED + "-x"):
        found = write_root_honesty.write_destinations(f"create hello.txt in {other} with the text hi")
        assert all(not item.startswith(SPACED) or item.startswith(other) for item in found), (other, found)
        assert SPACED not in found, (other, found)
    # Sentence punctuation after the folder still ends it.
    assert write_root_honesty.write_destinations(f"create hello.txt in {SPACED}.") == [SPACED]


@pytest.mark.parametrize("name", ["hello Ä.txt", "Ä.txt", "résumé.md"])
def test_the_normalizer_keeps_a_non_ascii_file_name_whole(name: str) -> None:
    """Candidate 4: the front door rewrote "hello Ä.txt" to "hello Ä. txt" on a resend."""
    assert name in normalize_user_text(f"create {name} in {SPACED} with the text hi").normalized_text
