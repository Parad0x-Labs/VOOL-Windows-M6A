"""Windows: an edit, an undo of it, and a move of a file the owner names inside a marked folder.

Captest round 1, R1-4 (ship bar M3), build 828b8bda on a clean Win11 VM, 2026-11-09. A new chat works
in the Desktop. Creating a file in a marked folder worked (the machine.write_file lane), but:

- editing a file in a marked folder answered "No text matches" (a search of the Desktop);
- editing a file with mixed CRLF/LF line endings answered "There is no file at mixed.txt";
- "undo the last change" acted on the Desktop workspace;
- moving a file between two marked folders answered "outside the active workspace (Desktop)".

What this file pins, through the production chat door (`dispatch_post`, a real `VoolAgent`, no model,
the chat unbound so its workspace is the Desktop) and at the lane's own reader:

- the edit lands in the named file inside the marked folder after the Manual approval, keeping every
  other byte (CRLF, LF, trailing spaces), for paths with spaces and `Ä`, in a folder outside Desktop,
  Documents and Downloads, both as a full file path and as a folder plus a file name;
- "undo the last change" restores the exact earlier bytes of that change, through the same approval;
  with no change VOOL made in a marked folder it says so plainly and changes nothing;
- a move between two marked folders lands; a move to a folder outside them is refused in plain words
  before any approval, and nothing moves;
- a symlink (a junction on Windows) inside a marked folder that leads out is judged by where it leads;
- a share or a drive Windows does not call local is never looked up; a turn that is not the owner's
  own local turn, and every turn off Windows, is left to the other lanes.

The Windows decisions are driven on Linux through the gate's `_IS_WINDOWS` flag and the marked folders
listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES, as the other marked-folder suites do. The move's live OS
confirmation (asked off Windows only; on Windows the Allow is the consent) is answered through
`os_consent_gate`'s test hook. Real `C:\\` spelling, NTFS junctions and
GetDriveTypeW are only exercised on a Windows host.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from core import windows_write_gate as gate
from tests.operator_profile_rig import body_json, in_request_thread, make_agent, profile_env_generator

MIXED = b"one\r\ntwo\nthree   \r\nfour\n"


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture()
def machine(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop, two marked folders with spaces and `Ä` outside Desktop,
    Documents and Downloads, and an unmarked folder next to them."""
    root = tmp_path_factory.mktemp("machine").resolve()
    home = root / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = root / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    first = root / "profile" / "VOOL Test" / "Allowed Ä folder"
    second = root / "profile" / "VOOL Test" / "Second Ä folder"
    unmarked = root / "profile" / "VOOL Test" / "Other folder"
    for folder in (first, second, unmarked):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(first), str(second)]))
    return {"home": home, "workspace": workspace, "first": first, "second": second, "unmarked": unmarked}


@pytest.fixture()
def consent(monkeypatch):
    """The move's live OS confirmation, answered yes; records what it was asked."""
    from core import os_consent_gate

    asked: list[str] = []

    def _yes(reason: str) -> bool:
        asked.append(reason)
        return True

    monkeypatch.setattr(os_consent_gate, "_TEST_OVERRIDE", _yes)
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


def _approve_and_resume(agent, text: str, session: str, workspace: Path, intent: str) -> tuple[str, str]:
    """The chat page's flow: the turn asks, the owner allows the card, the same turn is resent with its token."""
    from core.mode_permission_policy import resolve_approval

    first, turn_id = _chat(agent, text, session, workspace)
    pending = _pending(turn_id)
    assert len(pending) == 1, first
    assert pending[0].get("intent") == intent, pending[0]
    token = str(pending[0].get("approval_id"))
    assert resolve_approval(token, decision="allow") is not None
    resumed, _ = _chat(agent, text, session, workspace, turn_id=turn_id, token=token)
    return first, resumed


def _owner(**extra) -> dict:
    from core.request_trust import OWNER_LOCAL_KEY

    return {OWNER_LOCAL_KEY: True, "session_id": "unit-session", **extra}


# --- the lane's reader -------------------------------------------------------------------------------


def test_an_edit_naming_a_file_in_a_marked_folder_is_placed_in_that_folder(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    first = machine["first"]
    (first / "hello world.txt").write_bytes(b"hi there\n")
    for text in (
        f"replace 'hi' with 'hello' in {first / 'hello world.txt'}",
        f'replace "hi" with "hello" in "{first / "hello world.txt"}"',
        f"change \u201chi\u201d to \u201chello\u201d in {first / 'hello world.txt'}",
        f"in {first} replace 'hi' with 'hello' in \"hello world.txt\"",
    ):
        ask = marked_file_ask(text, source_context=_owner())
        assert ask is not None and not ask.refusal, (text, ask)
        assert (ask.intent, ask.workspace) == ("workspace.replace_in_file", str(first)), text
        assert ask.arguments == {"path": "hello world.txt", "old_text": "hi", "new_text": "hello"}, text


def test_the_lane_leaves_other_turns_alone(machine, monkeypatch) -> None:
    from core.request_trust import OWNER_LOCAL_KEY
    from core.windows_marked_file_ops import marked_file_ask

    (machine["first"] / "hello.txt").write_bytes(b"hi\n")
    text = f"replace 'hi' with 'hello' in {machine['first'] / 'hello.txt'}"
    assert marked_file_ask(text, source_context={OWNER_LOCAL_KEY: False}) is None  # not the owner's own turn
    assert marked_file_ask(text, source_context={}) is None
    assert marked_file_ask("replace 'hi' with 'hello' in hello.txt", source_context=_owner()) is None  # no full path
    assert marked_file_ask(f"what is in {machine['first'] / 'hello.txt'}", source_context=_owner()) is None
    assert marked_file_ask("undo the last change", source_context={OWNER_LOCAL_KEY: False}) is None
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    for words in (text, "undo the last change", f"move {machine['first'] / 'hello.txt'} to {machine['second']}"):
        assert marked_file_ask(words, source_context=_owner()) is None, words  # off Windows nothing changes


def test_an_unmarked_folder_is_refused_plainly(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    (machine["unmarked"] / "hello.txt").write_bytes(b"hi\n")
    ask = marked_file_ask(f"replace 'hi' with 'hello' in {machine['unmarked'] / 'hello.txt'}", source_context=_owner())
    assert ask is not None and not ask.intent
    assert "not inside a folder you marked" in ask.refusal and "Nothing was changed." in ask.refusal


def test_a_link_inside_a_marked_folder_that_leads_out_is_judged_by_where_it_leads(machine) -> None:
    """A junction on Windows (`mklink /J` needs no admin), a symlink here: by where it really leads."""
    from core.windows_marked_file_ops import marked_file_ask

    outside = machine["unmarked"] / "secret.txt"
    outside.write_bytes(b"hi\n")
    folder_link = machine["first"] / "escape"
    folder_link.symlink_to(machine["unmarked"], target_is_directory=True)
    file_link = machine["first"] / "secret link.txt"
    file_link.symlink_to(outside)
    for text in (
        f"replace 'hi' with 'hello' in {folder_link / 'secret.txt'}",
        f"replace 'hi' with 'hello' in {file_link}",
        f"in {folder_link} replace 'hi' with 'hello' in secret.txt",
    ):
        ask = marked_file_ask(text, source_context=_owner())
        assert ask is not None and not ask.intent and "Nothing was changed." in ask.refusal, (text, ask)
    (machine["first"] / "hello.txt").write_bytes(b"hi\n")
    ask = marked_file_ask(f"move {machine['first'] / 'hello.txt'} to {folder_link}", source_context=_owner())
    assert ask is not None and not ask.intent and "Nothing was moved." in ask.refusal
    assert outside.read_bytes() == b"hi\n" and not (machine["unmarked"] / "hello.txt").exists()


def test_a_drive_windows_does_not_call_local_is_never_looked_up(machine, monkeypatch) -> None:
    """A mapped network drive is a share under a letter: looking it up is the SMB connection. A drive
    GetDriveTypeW calls remote, or cannot place (None), is refused before any lookup."""
    from core import windows_known_folders
    from core import windows_marked_file_ops as marked
    from core.agent_runtime import write_root_honesty

    monkeypatch.setattr(marked, "_ON_WINDOWS", True)
    monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
    looked_up: list[str] = []
    real = {name: getattr(os.path, name) for name in ("exists", "lexists", "isdir", "isfile", "islink", "realpath")}

    def _watch(name):
        def _call(path, *args, **kwargs):
            if str(path).upper().startswith("Z:"):
                looked_up.append(f"{name}({path})")
            return real[name](path, *args, **kwargs)
        return _call

    for name in real:
        monkeypatch.setattr(os.path, name, _watch(name))
    for verdict in (None, True):
        monkeypatch.setattr(windows_known_folders, "_drive_is_remote", lambda _drive, _v=verdict: _v)
        for text in ("replace 'hi' with 'hello' in Z:\\VOOL Test\\hello.txt",
                     f"move Z:\\VOOL Test\\hello.txt to {machine['second']}"):
            ask = marked.marked_file_ask(text, source_context=_owner())
            assert ask is not None and not ask.intent and "don't look it up" in ask.refusal, (verdict, text, ask)
    assert looked_up == []


# --- the served chat: the edit ---------------------------------------------------------------------------


def test_the_edit_lands_in_the_marked_folder_after_the_approval(profile_env, machine) -> None:
    target = machine["first"] / "hello Ä.txt"
    target.write_bytes(b"hi there\n")
    (machine["workspace"] / "hello Ä.txt").write_bytes(b"hi there\n")
    agent = make_agent()
    first, resumed = _approve_and_resume(agent, f"replace 'hi' with 'hello' in {target}", "r14-edit",
                                         machine["workspace"], "workspace.replace_in_file")
    assert "requires approval" in first and str(target) in first, first
    assert "No text matches" not in first and "outside the active workspace" not in first
    assert target.read_bytes() == b"hello there\n", resumed
    assert (machine["workspace"] / "hello Ä.txt").read_bytes() == b"hi there\n"  # the Desktop is untouched


def test_the_mixed_line_ending_file_keeps_its_line_endings(profile_env, machine) -> None:
    target = machine["first"] / "mixed.txt"
    target.write_bytes(MIXED)
    agent = make_agent()
    first, resumed = _approve_and_resume(agent, f"in {machine['first']}, replace 'two' with 'TWO' in mixed.txt",
                                         "r14-mixed", machine["workspace"], "workspace.replace_in_file")
    assert "There is no file at" not in first and "There is no file at" not in resumed
    assert target.read_bytes() == b"one\r\nTWO\nthree   \r\nfour\n", resumed


def test_an_edit_in_an_unmarked_folder_is_refused_before_any_approval(profile_env, machine) -> None:
    target = machine["unmarked"] / "hello.txt"
    target.write_bytes(b"hi there\n")
    agent = make_agent()
    reply, turn_id = _chat(agent, f"replace 'hi' with 'hello' in {target}", "r14-unmarked", machine["workspace"])
    assert "not inside a folder you marked" in reply and "Nothing was changed." in reply, reply
    assert _pending(turn_id) == []
    assert target.read_bytes() == b"hi there\n"


# --- the served chat: the undo ---------------------------------------------------------------------------


def test_undo_restores_the_exact_earlier_bytes_of_the_edit(profile_env, machine) -> None:
    target = machine["first"] / "mixed.txt"
    target.write_bytes(MIXED)
    agent = make_agent()
    session = "r14-undo"
    _approve_and_resume(agent, f"replace 'three' with 'THREE' in {target}", session, machine["workspace"],
                        "workspace.replace_in_file")
    assert target.read_bytes() == b"one\r\ntwo\nTHREE   \r\nfour\n"
    first, resumed = _approve_and_resume(agent, "undo the last change", session, machine["workspace"],
                                         "workspace.rollback_last_change")
    assert "profile change" not in first and "requires approval" in first, first
    assert target.read_bytes() == MIXED, resumed
    # Nothing is left to undo now, and saying it again changes nothing.
    again, turn_id = _chat(agent, "undo the last change", session, machine["workspace"])
    assert "no change VOOL made in a marked folder" in again and "Nothing was changed." in again, again
    assert _pending(turn_id) == [] and target.read_bytes() == MIXED


def test_undo_with_no_change_is_refused_plainly(profile_env, machine) -> None:
    (machine["workspace"] / "notes.txt").write_bytes(b"keep\n")
    agent = make_agent()
    reply, turn_id = _chat(agent, "undo the last change", "r14-undo-none", machine["workspace"])
    assert "no change VOOL made in a marked folder" in reply and "Nothing was changed." in reply, reply
    assert _pending(turn_id) == []
    assert (machine["workspace"] / "notes.txt").read_bytes() == b"keep\n"


def test_undo_is_refused_once_the_folder_is_no_longer_marked(profile_env, machine, monkeypatch) -> None:
    target = machine["first"] / "hello.txt"
    target.write_bytes(b"hi there\n")
    agent = make_agent()
    session = "r14-undo-unmarked"
    _approve_and_resume(agent, f"replace 'hi' with 'hello' in {target}", session, machine["workspace"],
                        "workspace.replace_in_file")
    monkeypatch.setenv(gate.ENV_VAR, str(machine["second"]))
    reply, turn_id = _chat(agent, "undo the last change", session, machine["workspace"])
    assert "not a folder you have marked now" in reply and "Nothing was changed." in reply, reply
    assert _pending(turn_id) == [] and target.read_bytes() == b"hello there\n"


# --- the served chat: the move ---------------------------------------------------------------------------


def test_a_move_between_two_marked_folders_lands(profile_env, machine, consent, monkeypatch) -> None:
    from sandbox import job_runner

    # Windows as the exact approval's spend reads it (the job runner's seam): the Allow is the consent.
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: True)
    source = machine["first"] / "hello Ä.txt"
    source.write_bytes(MIXED)
    agent = make_agent()
    first, resumed = _approve_and_resume(agent, f"move {source} to {machine['second']}", "r14-move",
                                         machine["workspace"], "machine.move_path")
    assert "outside the active workspace" not in first and "requires approval" in first, first
    assert not source.exists(), resumed
    assert (machine["second"] / "hello Ä.txt").read_bytes() == MIXED
    assert consent == []  # on Windows the Allow on the card is the consent: no OS prompt (captest R4-1b)
    assert list(machine["workspace"].iterdir()) == []


@pytest.mark.parametrize("where", ["unmarked", "documents"])
def test_a_move_out_of_the_marked_folders_is_refused_and_nothing_moves(profile_env, machine, consent, where) -> None:
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    destination = machine["unmarked"] if where == "unmarked" else machine["home"] / "Documents"
    agent = make_agent()
    reply, turn_id = _chat(agent, f'move "{source}" into "{destination}"', f"r14-move-out-{where}", machine["workspace"])
    assert "outside the folders you marked" in reply and "Nothing was moved." in reply, reply
    assert _pending(turn_id) == [] and consent == []
    assert source.read_bytes() == b"hi\n" and not (destination / "hello.txt").exists()


def test_the_machine_mover_still_refuses_a_destination_outside_the_marked_folders(machine, consent) -> None:
    """The tool itself, as the model would call it: the gate refuses before anything moves."""
    from core.runtime_execution_tools import windows_write_refusal_before_decision

    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    refused = windows_write_refusal_before_decision(
        "machine.move_path", {"source": str(source), "destination": str(machine["unmarked"] / "hello.txt")},
        source_context={"workspace": str(machine["first"])},
    )
    assert refused is not None and not refused.ok
    assert source.exists() and consent == []
