"""Windows marked-folder lanes: the edit reader is anchored, and a resend after approval runs the approved call.

Security Architect conditions on the R1-4 car (2dfa88be / 489bbb52), 9 Nov 2026:

1. The edit reader searched the whole message, so a pasted log holding "replace 'a' with 'b' in <marked file>"
   planned an edit, and in Auto mode it would have run with no approval. Like "undo" and the program-request lane,
   the request must be one line that starts with replace or change, optionally after please, ok or now.
2. After the owner allows the card, the chat page resends the same turn. On that resend the text the front door
   received was the input normalizer's, which rewrote `hello Ä.txt` as `hello Ä. txt`. The resumed call must be
   exactly the approved one, read from what the user typed. The marked-folder write lane (machine.write_file) is
   checked the same way.

Windows is simulated as in the other marked-folder suites (the gate's `_IS_WINDOWS`, marked folders in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES, the served chat door with an unbound chat whose workspace is the Desktop).
"""
from __future__ import annotations

import pytest

from tests.operator_profile_rig import make_agent
from tests.test_windows_marked_folder_edit_undo_move import (
    _chat,
    _owner,
    _pending,
    consent,
    machine,
    profile_env,
)


def _approvals(turn_id: str) -> list[dict]:
    from core.mode_permission_policy import _APPROVALS

    return [a for a in _APPROVALS.values() if a.get("task_id") == turn_id]


def _allow_and_resend(agent, text: str, session: str, machine, intent: str, *, mode: str = "manual"):
    from core.mode_permission_policy import resolve_approval

    first, turn_id = _chat(agent, text, session, machine["workspace"], mode=mode)
    pending = _pending(turn_id)
    assert len(pending) == 1 and pending[0].get("intent") == intent, (first, pending)
    token = str(pending[0].get("approval_id"))
    assert resolve_approval(token, decision="allow") is not None
    resumed, _ = _chat(agent, text, session, machine["workspace"], turn_id=turn_id, token=token, mode=mode)
    return first, resumed, turn_id, token


# --- 1. the edit reader is anchored --------------------------------------------------------------------


def test_a_pasted_log_holding_an_edit_phrase_is_not_an_edit(machine) -> None:
    from core.windows_marked_file_ops import marked_file_ask

    target = machine["first"] / "hello.txt"
    target.write_bytes(b"a\n")
    for text in (
        f"Here is the log from yesterday:\n12:01 agent> replace 'a' with 'b' in {target}\n12:02 done\nwhat went wrong?",
        f"replace 'a' with 'b' in {target}\nand then tell me a joke",
        f"the log says replace 'a' with 'b' in {target}",
        f"why did you change 'a' to 'b' in {target}?",
    ):
        assert marked_file_ask(text, source_context=_owner()) is None, text
    for text in (f"replace 'a' with 'b' in {target}", f"please replace 'a' with 'b' in {target}",
                 f"ok, change 'a' to 'b' in {target}", f"Now replace 'a' with 'b' in {target}."):
        ask = marked_file_ask(text, source_context=_owner())
        assert ask is not None and ask.intent == "workspace.replace_in_file", text


def test_a_pasted_log_runs_no_tool_even_in_auto_mode(profile_env, machine) -> None:
    target = machine["first"] / "hello.txt"
    target.write_bytes(b"a\n")
    agent = make_agent()
    text = f"Here is the log:\n12:01 agent> replace 'a' with 'b' in {target}\n12:02 done\nwhat went wrong?"
    _reply, turn_id = _chat(agent, text, "anchor-auto", machine["workspace"], mode="auto")
    assert target.read_bytes() == b"a\n"
    assert _approvals(turn_id) == []


# --- 2. the resend after approval runs the approved call -----------------------------------------------


def test_the_resent_edit_runs_the_approved_call_on_the_name_as_typed(profile_env, machine) -> None:
    target = machine["first"] / "hello Ä.txt"
    target.write_bytes(b"hi there\n")
    agent = make_agent()
    first, resumed, turn_id, token = _allow_and_resend(
        agent, f"replace 'hi' with 'hello' in {target}", "resend-edit", machine, "workspace.replace_in_file")
    assert target.read_bytes() == b"hello there\n", resumed
    assert not (machine["first"] / "hello Ä. txt").exists()
    # The approved token was spent by the resumed call (same fingerprint), and no second card was raised.
    assert [a.get("status") for a in _approvals(turn_id)] == ["consumed"], _approvals(turn_id)


def test_the_resent_write_to_a_marked_folder_runs_the_approved_call(profile_env, machine) -> None:
    """The marked-folder write lane (machine.write_file) on the same resend."""
    agent = make_agent()
    first, resumed, turn_id, token = _allow_and_resend(
        agent, f'create "{machine["first"] / "hello Ä.txt"}" with the text hi', "resend-write", machine,
        "machine.write_file")
    assert (machine["first"] / "hello Ä.txt").read_text(encoding="utf-8").strip() == "hi", resumed
    assert not (machine["first"] / "hello Ä. txt").exists()
    assert [a.get("status") for a in _approvals(turn_id)] == ["consumed"], _approvals(turn_id)


@pytest.mark.parametrize("name", ["hello Ä.txt", "Ä.txt"])
def test_the_resent_move_runs_the_approved_call(profile_env, machine, consent, name) -> None:
    source = machine["first"] / name
    source.write_bytes(b"x\n")
    agent = make_agent()
    first, resumed, turn_id, token = _allow_and_resend(
        agent, f"move {source} to {machine['second']}", f"resend-move-{len(name)}", machine, "machine.move_path")
    assert (machine["second"] / name).read_bytes() == b"x\n" and not source.exists(), resumed
    assert [a.get("status") for a in _approvals(turn_id)] == ["consumed"], _approvals(turn_id)


def test_the_resent_write_reads_the_name_as_typed_not_the_normalized_text(profile_env, machine) -> None:
    """Found while checking condition 2: on the resend the normalizer's text read `hiÄ.txt` as `hi Ä. txt`, so the
    write lane read the folder `hi`, answered "I can write into .../hi, but only a plain text file", and the approved
    write never ran."""
    agent = make_agent()
    first, resumed, turn_id, token = _allow_and_resend(
        agent, f"create {machine['first'] / 'hiÄ.txt'} with the text hi", "resend-write-unquoted", machine,
        "machine.write_file")
    assert (machine["first"] / "hiÄ.txt").read_text(encoding="utf-8").strip() == "hi", resumed
    assert sorted(path.name for path in machine["first"].iterdir()) == ["hiÄ.txt"]
    assert [a.get("status") for a in _approvals(turn_id)] == ["consumed"], _approvals(turn_id)


def test_a_pasted_log_holding_a_move_phrase_is_not_a_move(machine) -> None:
    """The move reader is anchored the same way."""
    from core.windows_marked_file_ops import marked_file_ask

    source = machine["first"] / "hello.txt"
    source.write_bytes(b"x\n")
    for text in (
        f"Here is the log:\n12:01 agent> move {source} to {machine['second']}\nwhat went wrong?",
        f"the log says move {source} to {machine['second']}",
    ):
        assert marked_file_ask(text, source_context=_owner()) is None, text
    for text in (f"move {source} to {machine['second']}", f"please move the file {source} into {machine['second']}"):
        ask = marked_file_ask(text, source_context=_owner())
        assert ask is not None and ask.intent == "machine.move_path", text
