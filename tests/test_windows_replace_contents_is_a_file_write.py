"""Windows: "replace the contents of <file> with <text>" and "overwrite <file> with <text>" are file writes,
never a memory correction.

Real hardware, VOOL 0.7.1 (train 8bcb0ddf), Windows 11, the marked folder `C:\\Users\\Satoshi\\VOOL HW Test`
holding m3b.txt:

1. "replace the contents of C:\\Users\\Satoshi\\VOOL HW Test\\m3b.txt with the text v2" was answered by the memory
   command (route memory_command): "Correction applied. Current value: VOOL HW." It stored
   "Current corrected value: VOOL HW" in MEMORY.md and a memory_entries row keyed "fact:the:contents:users:satoshi",
   and left the file as it was. The memory correction reader takes any message that opens with "replace", and its
   value reader takes two capitalised words in a row ("VOOL HW") as the corrected value. The same words on the VM,
   whose path ("...\\VOOL Test\\Marked B\\m3b.txt") holds no two such words, found no value there and reached the
   marked-folder write.
2. "overwrite C:\\Users\\Satoshi\\VOOL HW Test\\m3b.txt with the text v2" reached the model, which answered "I'll
   overwrite the file ... Let me do that now." and wrote nothing. No lane read "overwrite <file> with <text>".

What this file pins, at the lane's reader, at the memory command and through the production chat door
(`dispatch_post`, a real `VoolAgent`, no model, the chat unbound so its workspace is the Desktop):

- both phrasings, for a file whose path has spaces, two capitalised words in a row and an `Ä`, ask for the
  approval of one `workspace.write_file` placed in the file's own folder, as "write the text <text> to <file>"
  does; on Allow the file holds exactly the text, and "undo that" restores the exact earlier bytes;
- neither phrasing is ever a memory correction: no memory entry is added and MEMORY.md does not change;
- a file outside the marked folders is refused in plain words before any approval, and no memory changes;
- a real correction ("actually my name is Sam") is still remembered;
- a file that is not there is answered in plain words (it is not left to the model);
- if the model is reached anyway, a promise to change a file on a turn where no tool ran ("I'll overwrite the
  file ... Let me do that now.") is dropped and the reply says no file was changed;
- off Windows the marked-folder lane, the memory command and the reply check read these messages as before.

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

PHRASES = (
    "replace the contents of {p} with the text v2",
    "overwrite {p} with the text v2",
)
READER_PHRASES = (
    *PHRASES,
    'replace the contents of "{p}" with the text v2',
    "please replace the contents of {p} with v2",
    'overwrite "{p}" with the text "v2"',
    "overwrite {p} with v2",
    "overwrite the file {p} with the text v2",
    "Overwrite {p} with the text v2.",
)


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture()
def machine(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop; marked folders named as on the hardware ("VOOL HW Test"), with two
    capitalised words in a row and spaces, and one with `Ä`; and an unmarked folder named the same way."""
    root = tmp_path_factory.mktemp("machine").resolve()
    home = root / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = root / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    hw = root / "Users" / "Satoshi" / "VOOL HW Test"
    umlaut = root / "Users" / "Satoshi" / "VOOL HW Ä Marked"
    unmarked = root / "Users" / "Satoshi" / "VOOL HW Other"
    for folder in (hw, umlaut, unmarked):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(hw), str(umlaut)]))
    return {"home": home, "workspace": workspace, "hw": hw, "umlaut": umlaut, "unmarked": unmarked}


def _chat(agent, text: str, session: str, workspace: Path, *, turn_id: str = "", token: str = "") -> tuple[str, str]:
    from core.web.api.runtime import RuntimeServices
    from core.web.api.service import dispatch_post

    body = {
        "messages": [{"role": "user", "content": text}],
        "session_id": session,
        "turn_id": turn_id or f"turn-{uuid.uuid4().hex[:10]}",
        "mode": "manual",
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


def _chat_memory(session_id: str) -> str:
    from core.context_namespace import ensure_chat_namespace

    ensure_chat_namespace(session_id)
    return session_id


def _memory_state() -> tuple[str, str]:
    """The canonical memory store and its MEMORY.md projection, as they are on disk now (both made first,
    from their templates, so a turn that only creates them is not counted as a change)."""
    from core.memory.files import memory_entries_path, memory_path
    from core.persistent_memory import ensure_memory_files

    ensure_memory_files()

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    return _read(memory_entries_path()), _read(memory_path())


def _assert_no_memory_written(before: tuple[str, str], reply: str) -> None:
    entries, mirror = _memory_state()
    assert "Correction applied" not in reply and "Current value" not in reply, reply
    assert entries == before[0], entries
    assert mirror == before[1], mirror
    assert "Current corrected" not in entries and "Current corrected" not in mirror
    assert "fact:the:contents" not in entries


def _files(machine) -> list[Path]:
    return [machine["hw"] / "m3b.txt", machine["umlaut"] / "Ä note.txt"]


# --- the lane's reader -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", READER_PHRASES)
def test_both_phrasings_are_one_write_placed_in_the_files_folder(machine, phrase) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    for target in _files(machine):
        target.write_bytes(b"VOOL HW first line\r\n")
        ask = marked_file_ask(phrase.format(p=target), source_context=_owner())
        assert ask is not None and not ask.refusal, (phrase, target, ask)
        assert (ask.kind, ask.intent, ask.workspace) == ("write", "workspace.write_file", str(target.parent)), ask
        assert ask.arguments == {
            "path": target.name, "content": "v2", "expected_hash": _sha(b"VOOL HW first line\r\n"),
        }, (phrase, ask.arguments)


@pytest.mark.parametrize("phrase", PHRASES)
def test_the_reader_asks_what_write_the_text_to_asks(machine, phrase) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    target = machine["hw"] / "m3b.txt"
    target.write_bytes(b"old")
    asked = marked_file_ask(phrase.format(p=target), source_context=_owner())
    plain = marked_file_ask(f"write the text v2 to {target}", source_context=_owner())
    assert asked == plain and asked is not None and asked.intent == "workspace.write_file", (asked, plain)


@pytest.mark.parametrize("phrase", PHRASES)
def test_outside_the_marks_both_phrasings_are_refused_by_the_reader(machine, phrase) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    outside = machine["unmarked"] / "m3b.txt"
    outside.write_bytes(b"old")
    ask = marked_file_ask(phrase.format(p=outside), source_context=_owner())
    assert ask is not None and not ask.intent, ask
    assert "not inside a folder you marked" in ask.refusal and "Nothing was changed." in ask.refusal, ask
    assert outside.read_bytes() == b"old"


@pytest.mark.parametrize("phrase", PHRASES)
def test_a_file_that_is_not_there_is_said_plainly_not_left_to_the_model(machine, phrase) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    missing = machine["hw"] / "not-there-yet.txt"
    ask = marked_file_ask(phrase.format(p=missing), source_context=_owner())
    assert ask is not None and not ask.intent and "There is no file at" in ask.refusal, ask
    assert f"write the text v2 to {missing}" in ask.refusal and "Nothing was changed." in ask.refusal, ask
    outside = machine["unmarked"] / "not-there-yet.txt"
    ask = marked_file_ask(phrase.format(p=outside), source_context=_owner())
    assert ask is not None and not ask.intent and "not inside a folder you marked" in ask.refusal, ask
    assert not missing.exists() and not outside.exists()


# --- the memory command -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", READER_PHRASES)
def test_the_memory_command_never_takes_a_file_write(profile_env, machine, phrase) -> None:
    from core.persistent_memory import maybe_handle_memory_command

    for target in [*_files(machine), machine["unmarked"] / "m3b.txt", machine["hw"] / "not-there-yet.txt"]:
        if target.name != "not-there-yet.txt":
            target.write_bytes(b"old")
        before = _memory_state()
        handled, response = maybe_handle_memory_command(phrase.format(p=target), session_id=_chat_memory("mem-file-subject"))
        assert (handled, response) == (False, ""), (phrase, target, response)
        _assert_no_memory_written(before, response)


def test_a_correction_whose_subject_is_a_file_path_is_not_a_memory_correction(profile_env, machine) -> None:
    from core.persistent_memory import maybe_handle_memory_command

    target = machine["hw"] / "m3b.txt"
    target.write_bytes(b"old")
    for text in (
        f"replace {target} with the text VOOL HW",
        f"update {target} with the text NEW VALUE",
        f"correction: {target} should say VOOL HW",
    ):
        before = _memory_state()
        handled, response = maybe_handle_memory_command(text, session_id=_chat_memory("mem-file-subject-2"))
        assert (handled, response) == (False, ""), (text, response)
        _assert_no_memory_written(before, response)


def test_a_real_correction_is_still_a_memory_correction_on_windows(profile_env, machine) -> None:
    from core.context_scope import CurrentTurnCorrection, current_turn_corrections
    from core.persistent_memory import maybe_handle_memory_command

    session = _chat_memory("mem-real")
    assert current_turn_corrections("actually my name is Sam") == (CurrentTurnCorrection(fact_key="name", value="Sam"),)
    for text, value in (
        ("Correction: my exact installer verification identifier is INSTALL-MEM-E31-9842.", "INSTALL-MEM-E31-9842"),
        ("Correction: my test label is VOOL HW", "VOOL HW"),
        ("update my route label to ROUTE-A7", "ROUTE-A7"),
    ):
        before = _memory_state()
        handled, response = maybe_handle_memory_command(text, session_id=session)
        assert handled is True and "Correction applied" in response and value in response, (text, response)
        assert _memory_state()[0] != before[0] and value in _memory_state()[0], text


# --- the served chat ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", PHRASES)
@pytest.mark.parametrize("which", [0, 1], ids=["hw_path", "umlaut_path"])
def test_served_write_lands_after_the_card_and_writes_no_memory(profile_env, machine, phrase, which) -> None:
    target = _files(machine)[which]
    target.write_bytes(b"VOOL HW first line\r\n")
    (machine["workspace"] / target.name).write_bytes(b"desktop")
    agent = make_agent()
    session = f"replace-write-{uuid.uuid4().hex[:6]}"
    before = _memory_state()
    first, resumed, card = _allow(agent, phrase.format(p=target), session, machine["workspace"], "workspace.write_file")
    assert "requires approval" in first and str(target) in first, first
    assert "Let me do that now" not in first and "Let me do that now" not in resumed, (first, resumed)
    assert card.get("reversible") is True, card
    assert target.read_bytes() == b"v2", resumed
    assert (machine["workspace"] / target.name).read_bytes() == b"desktop"  # the Desktop is untouched
    _assert_no_memory_written(before, first + resumed)


@pytest.mark.parametrize("phrase", PHRASES)
def test_served_undo_restores_the_exact_earlier_bytes(profile_env, machine, phrase) -> None:
    target = machine["hw"] / "m3b.txt"
    earlier = b"\xef\xbb\xbfVOOL HW one\r\ntwo\r\n"
    target.write_bytes(earlier)
    agent = make_agent()
    session = f"replace-undo-{uuid.uuid4().hex[:6]}"
    before = _memory_state()
    _first, resumed, _card = _allow(agent, phrase.format(p=target), session, machine["workspace"],
                                    "workspace.write_file")
    assert target.read_bytes() == b"v2", resumed
    _f, undone, _card = _allow(agent, "undo that", session, machine["workspace"], "workspace.rollback_last_change")
    assert target.read_bytes() == earlier, undone
    _assert_no_memory_written(before, resumed + undone)


@pytest.mark.parametrize("phrase", PHRASES)
def test_served_write_outside_the_marks_is_refused_before_any_card(profile_env, machine, phrase) -> None:
    outside = machine["unmarked"] / "m3b.txt"
    outside.write_bytes(b"VOOL HW first line")
    agent = make_agent()
    before = _memory_state()
    reply, turn_id = _chat(agent, phrase.format(p=outside), f"replace-out-{uuid.uuid4().hex[:6]}",
                           machine["workspace"])
    assert "not inside a folder you marked" in reply and "Nothing was changed." in reply, reply
    assert _pending(turn_id) == [] and outside.read_bytes() == b"VOOL HW first line"
    _assert_no_memory_written(before, reply)


@pytest.mark.parametrize("phrase", PHRASES)
def test_served_write_of_a_file_that_is_not_there_is_answered_plainly(profile_env, machine, phrase) -> None:
    missing = machine["hw"] / "not-there-yet.txt"
    agent = make_agent()
    before = _memory_state()
    reply, turn_id = _chat(agent, phrase.format(p=missing), f"replace-missing-{uuid.uuid4().hex[:6]}",
                           machine["workspace"])
    assert "There is no file at" in reply and "Nothing was changed." in reply, reply
    assert "Let me do that now" not in reply and _pending(turn_id) == [] and not missing.exists()
    _assert_no_memory_written(before, reply)


def test_served_real_correction_is_still_a_correction(profile_env, machine) -> None:
    """After a file write in the same chat, a remembered name is saved as before, "actually my name is Sam"
    is read as this turn's own correction of it (as on base: no deterministic lane answers it, the model
    does, told of the correction), and a typed memory correction is still applied by the memory command."""
    from core.context_scope import CurrentTurnCorrection, current_turn_corrections

    target = machine["hw"] / "m3b.txt"
    target.write_bytes(b"old")
    agent = make_agent()
    session = f"real-correction-{uuid.uuid4().hex[:6]}"
    _allow(agent, f"replace the contents of {target} with the text v2", session, machine["workspace"],
           "workspace.write_file")
    assert target.read_bytes() == b"v2"
    saved, turn_id = _chat(agent, "remember that my name is Alex", session, machine["workspace"])
    assert _pending(turn_id) == [] and "Alex" in saved, saved
    reply, turn_id = _chat(agent, "actually my name is Sam", session, machine["workspace"])
    assert _pending(turn_id) == [] and "Correction applied" not in reply and target.read_bytes() == b"v2", reply
    assert current_turn_corrections("actually my name is Sam") == (CurrentTurnCorrection(fact_key="name", value="Sam"),)
    before = _memory_state()
    corrected, turn_id = _chat(agent, "Correction: my test label is VOOL HW", session, machine["workspace"])
    assert _pending(turn_id) == [] and "Correction applied" in corrected and "VOOL HW" in corrected, corrected
    assert _memory_state()[0] != before[0] and "Current corrected label: VOOL HW" in _memory_state()[0]


# --- the model's reply, when one is reached --------------------------------------------------------------------


PROMISE = "I'll overwrite the file {p} with the text v2. Let me do that now."


def test_a_promise_to_change_a_file_with_no_tool_run_is_made_honest(machine) -> None:
    from core.agent_runtime.action_honesty_validator import enforce_final_action_honesty

    target = machine["hw"] / "m3b.txt"
    for response in (PROMISE.format(p=target), f"Sure. I'll replace the contents of {target} with v2.\nLet me do that now."):
        out = enforce_final_action_honesty({"response": response}, user_input=f"overwrite {target} with the text v2",
                                           session_id="honesty-unit", source_context={})
        text = str(out.get("response") or "")
        assert "Let me do that now" not in text and "I'll overwrite" not in text and "I'll replace" not in text, text
        assert "did not change any file" in text or "No file was changed" in text, text
        assert (out.get("action_honesty_validator") or {}).get("reason") == "unbacked_file_change_promise_removed"


def test_an_offer_a_tool_record_or_another_platform_keeps_the_reply(machine, monkeypatch) -> None:
    from core.agent_runtime.action_honesty_validator import enforce_final_action_honesty

    target = machine["hw"] / "m3b.txt"
    ask = f"overwrite {target} with the text v2"
    offer = f"If you want, I'll overwrite the file {target} with v2."
    assert enforce_final_action_honesty({"response": offer}, user_input=ask, session_id="h1",
                                        source_context={})["response"] == offer
    queued = {"tool_receipts": [{"tool_name": "workspace.write_file", "status": "pending_approval"}]}
    assert enforce_final_action_honesty({"response": PROMISE.format(p=target)}, user_input=ask, session_id="h2",
                                        source_context=queued)["response"] == PROMISE.format(p=target)
    code = "I'll write a Python function that reads the file line by line."
    assert enforce_final_action_honesty({"response": code}, user_input="how do I read a file in python?",
                                        session_id="h3", source_context={})["response"] == code
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert enforce_final_action_honesty({"response": PROMISE.format(p=target)}, user_input=ask, session_id="h4",
                                        source_context={})["response"] == PROMISE.format(p=target)


# --- off Windows ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", PHRASES)
def test_off_windows_the_marked_lane_leaves_both_phrasings_alone(machine, monkeypatch, phrase) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    target = machine["hw"] / "m3b.txt"
    target.write_bytes(b"old")
    assert marked_file_ask(phrase.format(p=target), source_context=_owner()) is None


def test_off_windows_the_memory_command_reads_these_messages_as_before(profile_env, machine, monkeypatch) -> None:
    """Off Windows nothing here changes: the memory command reads "replace ..." as on base (this fix is
    behind the Windows seam), and "overwrite ..." is no memory command there either."""
    from core.persistent_memory import maybe_handle_memory_command

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    target = machine["hw"] / "m3b.txt"
    target.write_bytes(b"old")
    handled, response = maybe_handle_memory_command(
        f"replace the contents of {target} with the text v2", session_id=_chat_memory("off-windows-replace"),
    )
    assert handled is True and "Correction applied" in response, response
    assert maybe_handle_memory_command(f"overwrite {target} with the text v2", session_id=_chat_memory("off-windows-o")) == (False, "")
