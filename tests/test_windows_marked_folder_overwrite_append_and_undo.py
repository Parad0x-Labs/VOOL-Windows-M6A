"""Windows: an owner's overwrite or append of a file in a marked folder, and "undo that" after a create, an
overwrite, an append or a move there (ship bar M3).

Captest VM, build b58c5eb0 (clean Win11, Manual mode, marks include `...\\VOOL Test\\Marked B` and
`...\\VOOL Test\\Allowed Ä folder`, the chat's own folder the Desktop):

1. "create undo-r5.txt in <Marked B> with the text u" raised a machine.write_file card; Allow wrote it through
   the broker. "undo that" in the same chat answered (windows_marked_file_undo) "There is no change VOOL made in
   a marked folder in this chat that is left to undo." A move between two marked folders (broker rename: done)
   got the same answer. The undo lane reads the chat's change ledger; machine.write_file and machine.move_path
   never recorded in it.
2. "write the text v2 to <marked>\\note.txt" and "append the line 'edited by VOOL' to <marked>\\note.txt" were
   refused by machine_write_guard ("only a plain text file with the exact text given"); "replace the contents
   of <marked>\\note.txt with the text v2" went to the model, which split the path at the space.

What this file pins, through the production chat door (`dispatch_post`, a real `VoolAgent`, no model, the chat
unbound so its workspace is the Desktop) and at the lane's own reader:

- an overwrite ("write the text X to <file>", "replace the contents of <file> with the text X") and an append
  ("append the line X to <file>") of an existing file in a marked folder ask for the approval of one
  `workspace.write_file` placed in that file's own folder, land on Allow with exactly the asked bytes (an
  append keeps the file's own line endings), and "undo that" restores the exact earlier bytes;
- "undo that" after a create in a marked folder asks for approval and removes the created file;
- "undo that" after a move between two marked folders asks for approval and moves the file back; after it
  nothing is left to undo;
- the machine writer, routed through the broker, records the change with the bytes the broker wrote;
- an unmarked folder, a link out, a `..` segment, a share and a drive Windows does not call local are refused
  in plain words before any approval; off Windows and on a turn that is not the owner's, nothing changes.

Windows is simulated as in the other marked-folder suites (the gate's `_IS_WINDOWS`, marked folders listed in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES).
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from tests.operator_profile_rig import body_json, in_request_thread, make_agent, profile_env_generator


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture()
def machine(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop, two marked folders with spaces (one with `Ä`) outside Desktop,
    Documents and Downloads, and an unmarked folder next to them."""
    root = tmp_path_factory.mktemp("machine").resolve()
    home = root / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = root / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    first = root / "profile" / "VOOL Test" / "Allowed Ä folder"
    second = root / "profile" / "VOOL Test" / "Marked B"
    unmarked = root / "profile" / "VOOL Test" / "Other folder"
    for folder in (first, second, unmarked):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(first), str(second)]))
    return {"home": home, "workspace": workspace, "first": first, "second": second, "unmarked": unmarked}


@pytest.fixture()
def windows_move(monkeypatch):
    """The move as Windows spends it: the Allow on the card is the consent (the job runner's seam), and
    an OS prompt would be recorded here."""
    from core import os_consent_gate
    from sandbox import job_runner

    asked: list[str] = []

    def _yes(reason: str) -> bool:
        asked.append(reason)
        return True

    monkeypatch.setattr(os_consent_gate, "_TEST_OVERRIDE", _yes)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    return asked


def _chat(agent, text: str, session: str, workspace: Path, *, turn_id: str = "", token: str = "",
          mode: str = "manual") -> tuple[str, str]:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    body = {
        "messages": [{"role": "user", "content": text}],
        "session_id": session,
        "turn_id": turn_id or f"turn-{uuid.uuid4().hex[:10]}",
        "mode": mode,
    }
    if token:
        body["approval_token"] = token

    def _dispatch():
        response = dispatch_post(
            path="/api/chat",
            body=body,
            headers={"Host": "127.0.0.1", "Content-Type": "application/json"},
            runtime=RuntimeServices(display_name="VOOL", agent=agent),
            model_name="test-backend",
            workspace_root_provider=lambda: str(workspace),
            client_host="127.0.0.1",
            request_id=f"auto-{uuid.uuid4().hex}",
        )
        if getattr(response, "stream", None) is not None and not response.body:
            response.stream = iter(list(response.stream))
        return response

    payload = body_json(in_request_thread(_dispatch))
    return str(((payload.get("message") or {}).get("content")) or ""), body["turn_id"]


def _pending(turn_id: str) -> list[dict]:
    from core.mode_permission_policy import _APPROVALS

    return [a for a in _APPROVALS.values() if a.get("status") == "pending" and a.get("task_id") == turn_id]


def _allow(agent, text: str, session: str, workspace: Path, intent: str) -> tuple[str, str, dict]:
    """The chat page's flow: the turn asks, the owner allows the card, the same turn is resent with its token."""
    from core.mode_permission_policy import resolve_approval

    first, turn_id = _chat(agent, text, session, workspace)
    pending = _pending(turn_id)
    assert len(pending) == 1, first
    assert pending[0].get("intent") == intent, pending[0]
    card = dict(pending[0])
    token = str(card.get("approval_id"))
    assert resolve_approval(token, decision="allow") is not None
    resumed, _ = _chat(agent, text, session, workspace, turn_id=turn_id, token=token)
    return first, resumed, card


def _owner(**extra) -> dict:
    from core.request_trust import OWNER_LOCAL_KEY

    return {OWNER_LOCAL_KEY: True, "session_id": "unit-session", **extra}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the lane's reader: overwrite and append --------------------------------------------------------------


def test_an_overwrite_of_a_marked_file_is_one_write_placed_in_its_folder(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    first = machine["first"]
    note = first / "note.txt"
    note.write_bytes(b"first line")
    for text in (
        f"write the text v2 to {note}",
        f'write the text "v2" to "{note}"',
        f"please write the text 'v2' into {note}",
        f"put the text v2 in {note}",
        f"replace the contents of {note} with the text v2",
        f'replace the contents of "{note}" with "v2"',
        f"replace the text in {note} with v2",
    ):
        ask = marked_file_ask(text, source_context=_owner())
        assert ask is not None and not ask.refusal, (text, ask)
        assert (ask.kind, ask.intent, ask.workspace) == ("write", "workspace.write_file", str(first)), text
        assert ask.arguments == {"path": "note.txt", "content": "v2", "expected_hash": _sha(b"first line")}, text


def test_an_append_keeps_the_files_own_line_endings(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    first = machine["first"]
    for before, after in (
        (b"first line", b"first line\nedited by VOOL"),
        (b"first line\n", b"first line\nedited by VOOL\n"),
        (b"one\r\ntwo\r\n", b"one\r\ntwo\r\nedited by VOOL\r\n"),
        (b"", b"edited by VOOL"),
    ):
        note = first / "note.txt"
        note.write_bytes(before)
        for text in (f"append the line 'edited by VOOL' to {note}", f'append the line "edited by VOOL" to "{note}"'):
            ask = marked_file_ask(text, source_context=_owner())
            assert ask is not None and not ask.refusal, (text, ask)
            assert (ask.intent, ask.workspace) == ("workspace.write_file", str(first)), text
            assert ask.arguments == {"path": "note.txt", "content": after.decode(), "expected_hash": _sha(before)}, (
                before, ask.arguments)


def test_an_overwrite_or_append_outside_the_marks_is_refused_plainly(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    outside = machine["unmarked"] / "note.txt"
    outside.write_bytes(b"first line")
    folder_link = machine["first"] / "escape"
    folder_link.symlink_to(machine["unmarked"], target_is_directory=True)
    file_link = machine["first"] / "link.txt"
    file_link.symlink_to(outside)
    (machine["first"] / "sub").mkdir()
    (machine["first"] / "note.txt").write_bytes(b"first line")
    climb = f"{machine['first']}{os.sep}sub{os.sep}..{os.sep}note.txt"
    climb_out = f"{machine['first']}{os.sep}..{os.sep}Other folder{os.sep}note.txt"
    for path in (outside, folder_link / "note.txt", file_link, climb, climb_out):
        for text in (f"write the text v2 to {path}", f"append the line 'x' to {path}",
                     f"replace the contents of {path} with the text v2"):
            ask = marked_file_ask(text, source_context=_owner())
            assert ask is not None and not ask.intent and "Nothing was changed." in ask.refusal, (text, ask)
    assert outside.read_bytes() == b"first line" and (machine["first"] / "note.txt").read_bytes() == b"first line"


def test_a_share_or_a_drive_windows_does_not_call_local_is_never_looked_up(machine, monkeypatch) -> None:
    from core import windows_known_folders
    from core import windows_marked_file_ops as marked
    from core.agent_runtime import write_root_honesty

    monkeypatch.setattr(marked, "_ON_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    looked_up: list[str] = []
    real = {name: getattr(os.path, name) for name in ("exists", "lexists", "isdir", "isfile", "islink", "realpath")}

    def _watch(name):
        def _call(path, *args, **kwargs):
            if str(path).upper().startswith(("Z:", "\\\\")):
                looked_up.append(f"{name}({path})")
            return real[name](path, *args, **kwargs)
        return _call

    for name in real:
        monkeypatch.setattr(os.path, name, _watch(name))
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda _drive: True)
    for path in ("Z:\\VOOL Test\\note.txt", "\\\\server\\share\\note.txt"):
        for text in (f"write the text v2 to {path}", f"append the line 'x' to {path}"):
            ask = marked.marked_file_ask(text, source_context=_owner())
            assert ask is not None and not ask.intent and "don't look it up" in ask.refusal, (text, ask)
    assert looked_up == []


def test_a_non_utf8_file_is_not_appended_to(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    note = machine["first"] / "note.txt"
    note.write_bytes(b"caf\xe9\n")
    ask = marked_file_ask(f"append the line 'x' to {note}", source_context=_owner())
    assert ask is not None and not ask.intent and "Nothing was changed." in ask.refusal, ask


def test_the_write_reader_leaves_other_turns_alone(machine, monkeypatch) -> None:
    from core.request_trust import OWNER_LOCAL_KEY
    from core.windows_marked_file_ops import marked_file_ask

    note = machine["first"] / "note.txt"
    note.write_bytes(b"first line")
    text = f"write the text v2 to {note}"
    assert marked_file_ask(text, source_context={OWNER_LOCAL_KEY: False}) is None
    assert marked_file_ask(f"Here is a log:\n{text}", source_context=_owner()) is None
    assert marked_file_ask(f"the log says {text}", source_context=_owner()) is None
    # A file that is not there yet is a create: the machine.write_file lane's, as before.
    assert marked_file_ask(f"write the text v2 to {machine['first'] / 'new.txt'}", source_context=_owner()) is None
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    for words in (text, f"append the line 'x' to {note}"):
        assert marked_file_ask(words, source_context=_owner()) is None, words


# --- the machine writer and mover keep the chat's change record ----------------------------------------------


def test_the_brokered_machine_write_records_the_bytes_the_broker_wrote(tmp_path, monkeypatch) -> None:
    from core import runtime_execution_tools as tools
    from core.execution.artifacts import latest_workspace_mutation_for_session
    from core.windows_broker import session
    from tests.windows_broker.test_single_writes import _Broker, _wire

    folder = (tmp_path / "flagged").resolve()
    folder.mkdir()
    (folder / "note.txt").write_bytes(b"first line\r\n")
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(folder))
    monkeypatch.setattr(session, "_host", None)
    fake = _Broker(folder)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(tools, "_resolve_machine_directory", lambda raw, write=False: folder / str(raw))
    monkeypatch.setattr(tools.ExecutionGate, "evaluate_machine_effect", staticmethod(lambda **kw: {"decision": "authorized"}))
    result = tools._write_machine_file({"path": "note.txt", "content": "v2\nmore"}, session_id="r5-broker")
    assert result.ok, result.response_text
    record = latest_workspace_mutation_for_session("r5-broker")
    assert record is not None and record["workspace_root"] == str(folder) and record["intent"] == "machine.write_file"
    (change,) = record["changes"]
    assert change["path"] == "note.txt" and change["existed_before"] is True
    assert change["before_text"] == "first line\r\n"
    assert change["after_hash"] == _sha(b"v2\r\nmore")  # the broker's bytes: "\n" written as "\r\n"


def test_a_machine_write_outside_the_marks_records_nothing(tmp_path, monkeypatch) -> None:
    from core import runtime_execution_tools as tools
    from core.execution.artifacts import latest_workspace_mutation_for_session

    folder = (tmp_path / "plain").resolve()
    folder.mkdir()
    monkeypatch.setattr(tools, "_resolve_machine_directory", lambda raw, write=False: folder / str(raw))
    monkeypatch.setattr(tools.ExecutionGate, "evaluate_machine_effect", staticmethod(lambda **kw: {"decision": "authorized"}))
    result = tools._write_machine_file({"path": "a.txt", "content": "hi"}, session_id="r5-plain")
    assert result.ok and (folder / "a.txt").read_bytes() == b"hi"
    assert latest_workspace_mutation_for_session("r5-plain") is None


# --- the served chat ------------------------------------------------------------------------------------------


def test_undo_after_a_create_in_a_marked_folder_removes_the_file(profile_env, machine) -> None:
    target = machine["second"] / "undo-r5.txt"
    agent = make_agent()
    session = "r5-create-undo"
    _first, resumed, _card = _allow(agent, f"create undo-r5.txt in {machine['second']} with the text u", session,
                                    machine["workspace"], "machine.write_file")
    assert target.read_bytes() == b"u", resumed
    first, resumed, _card = _allow(agent, "undo that", session, machine["workspace"], "workspace.rollback_last_change")
    assert "no change VOOL made in a marked folder" not in first and "requires approval" in first, first
    assert not target.exists(), resumed
    again, turn_id = _chat(agent, "undo that", session, machine["workspace"])
    assert "no change VOOL made in a marked folder" in again and "Nothing was changed." in again, again
    assert _pending(turn_id) == []


@pytest.mark.parametrize("phrase", ["write the text v2 to {p}", "replace the contents of {p} with the text v2"])
def test_an_overwrite_lands_after_the_card_and_undo_restores_the_old_text(profile_env, machine, phrase) -> None:
    note = machine["first"] / "note.txt"
    note.write_bytes(b"first line")
    (machine["workspace"] / "note.txt").write_bytes(b"desktop")
    agent = make_agent()
    session = f"r5-overwrite-{uuid.uuid4().hex[:6]}"
    first, resumed, card = _allow(agent, phrase.format(p=note), session, machine["workspace"], "workspace.write_file")
    assert "requires approval" in first and str(note) in first, first
    assert "only a plain text file" not in first and "outside the active workspace" not in first, first
    assert card.get("reversible") is True, card
    assert note.read_bytes() == b"v2", resumed
    assert (machine["workspace"] / "note.txt").read_bytes() == b"desktop"  # the Desktop is untouched
    _f, resumed, _card = _allow(agent, "undo that", session, machine["workspace"], "workspace.rollback_last_change")
    assert note.read_bytes() == b"first line", resumed


def test_an_append_lands_after_the_card_and_undo_restores_the_old_bytes(profile_env, machine) -> None:
    note = machine["first"] / "note.txt"
    note.write_bytes(b"one\r\ntwo\r\n")
    agent = make_agent()
    session = "r5-append"
    first, resumed, card = _allow(agent, f"append the line 'edited by VOOL' to {note}", session, machine["workspace"],
                                  "workspace.write_file")
    assert "requires approval" in first and card.get("reversible") is True, (first, card)
    assert note.read_bytes() == b"one\r\ntwo\r\nedited by VOOL\r\n", resumed
    _f, resumed, _card = _allow(agent, "undo that", session, machine["workspace"], "workspace.rollback_last_change")
    assert note.read_bytes() == b"one\r\ntwo\r\n", resumed


def test_an_overwrite_outside_the_marks_is_refused_before_any_card(profile_env, machine) -> None:
    note = machine["unmarked"] / "note.txt"
    note.write_bytes(b"first line")
    agent = make_agent()
    reply, turn_id = _chat(agent, f"write the text v2 to {note}", "r5-overwrite-out", machine["workspace"])
    assert "not inside a folder you marked" in reply and "Nothing was changed." in reply, reply
    assert _pending(turn_id) == [] and note.read_bytes() == b"first line"


def test_undo_after_a_move_between_marked_folders_moves_it_back(profile_env, machine, windows_move) -> None:
    source = machine["first"] / "mv-r6.txt"
    source.write_bytes(b"move me\r\n")
    moved = machine["second"] / "mv-r6.txt"
    agent = make_agent()
    session = "r6-move-undo"
    _f, resumed, _card = _allow(agent, f"move {source} to {moved}", session, machine["workspace"], "machine.move_path")
    assert not source.exists() and moved.read_bytes() == b"move me\r\n", resumed
    first, resumed, card = _allow(agent, "undo that", session, machine["workspace"], "machine.move_path")
    assert "no change VOOL made in a marked folder" not in first and "requires approval" in first, first
    assert str(moved) in str(card.get("affected_resources")) and str(source) in str(card.get("affected_resources")), card
    assert source.read_bytes() == b"move me\r\n" and not moved.exists(), resumed
    assert windows_move == []  # the Allow is the consent on Windows
    again, turn_id = _chat(agent, "undo that", session, machine["workspace"])
    assert "no change VOOL made in a marked folder" in again and "Nothing was changed." in again, again
    assert _pending(turn_id) == [] and source.exists() and not moved.exists()


def test_undo_of_a_move_is_refused_when_its_place_is_taken(profile_env, machine, windows_move) -> None:
    source = machine["first"] / "mv-r6.txt"
    source.write_bytes(b"move me\n")
    moved = machine["second"] / "mv-r6.txt"
    agent = make_agent()
    session = "r6-move-undo-taken"
    _allow(agent, f"move {source} to {machine['second']}", session, machine["workspace"], "machine.move_path")
    assert moved.exists() and not source.exists()
    source.write_bytes(b"someone else\n")
    reply, turn_id = _chat(agent, "undo that", session, machine["workspace"])
    assert "already exists" in reply and "Nothing was" in reply, reply
    assert _pending(turn_id) == []
    assert source.read_bytes() == b"someone else\n" and moved.read_bytes() == b"move me\n"
