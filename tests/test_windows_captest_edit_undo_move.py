"""Windows 0.7.1-beta capability test, marked folder: a quoted replace, an undo, and a move into a new folder.

Seen in the installed Windows 0.7.1-beta (clean VM, a chat in a folder marked in
VOOL_WINDOWS_DISPOSABLE_WORKSPACES, after a restart):

- B8: "replace 'hi' with 'hello' in hello.txt" only asked to confirm and no edit landed; "undo the last change"
  was answered by the profile lane ("There is no profile change to undo.") instead of rolling the edit back.
- B12: "make folder sub and move hello.txt into it" ended with the Windows gate's internal sentence
  "operator.move_path names no destination_path, so its target cannot be checked".

What this file pins, through the production chat door (`dispatch_post` with a real `VoolAgent`, no model) where a
lane decides, and at the owning function where it does not:

- A replacement whose two values are written in plain or typographic quotes is planned exactly as the code-span
  form is: the Manual approval names the file, approving it and resending the turn (what the chat page does) edits
  the file, and the bytes around the replaced text are kept as they were, CRLF and trailing spaces included.
- "undo the last change" after a tracked workspace change is not claimed by the profile lane, and the words seat
  `workspace.rollback_last_change` for the tool offer. A profile change newer than the chat's last workspace change
  is still undone by the profile lane.
- On Windows an operator move request the move handler reads no move from is answered by the handler
  (`invalid_request`, nothing moved), not refused by the gate on the request's raw fields; every move the handler
  does read is still gated, including the archive default outside the marked folders.

The Windows gate is switched with `windows_write_gate._IS_WINDOWS` and the marked folder set in the environment, as
the gate's own suites do. Real Windows path spelling (`C:\\...`) is only exercised on a Windows host.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core import windows_write_gate
from core.execution.planner import _explicit_replace_request
from tests.operator_profile_rig import body_json, in_request_thread, make_agent, profile_env_generator


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


@pytest.fixture()
def marked(tmp_path_factory, monkeypatch):
    folder = tmp_path_factory.mktemp("marked").resolve()
    monkeypatch.setattr(windows_write_gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(windows_write_gate.ENV_VAR, str(folder))
    return folder


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


def _approve_and_resume(agent, text: str, session: str, workspace: Path) -> tuple[str, str]:
    """The chat page's flow: the turn asks, the operator allows the card, the same turn is resent with its token."""
    from core.mode_permission_policy import _APPROVALS, resolve_approval

    first, turn_id = _chat(agent, text, session, workspace)
    pending = [a for a in _APPROVALS.values() if a.get("status") == "pending" and a.get("task_id") == turn_id]
    assert len(pending) == 1, first
    assert pending[0].get("intent") == "workspace.replace_in_file", pending[0]
    token = str(pending[0].get("approval_id"))
    assert resolve_approval(token, decision="allow") is not None
    resumed, _ = _chat(agent, text, session, workspace, turn_id=turn_id, token=token)
    return first, resumed


# --- B8: the quoted replace -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "replace 'hi' with 'hello' in hello.txt",
        'replace "hi" with "hello" in hello.txt',
        "replace \u2018hi\u2019 with \u2018hello\u2019 in hello.txt",
        "replace \u201chi\u201d with \u201chello\u201d in hello.txt",
        "replace `hi` with `hello` in hello.txt",
    ],
)
def test_a_quoted_replacement_is_read_like_a_code_span(request_text):
    assert _explicit_replace_request(request_text) == {"old_text": "hi", "new_text": "hello", "path": "hello.txt"}


def test_a_quoted_value_keeps_its_inner_spaces_and_other_quote_marks():
    assert _explicit_replace_request('replace "it\'s here" with "it is here" in notes.md') == {
        "old_text": "it's here", "new_text": "it is here", "path": "notes.md",
    }
    # A value opened with one mark and closed with another is no value: nothing is planned from it.
    assert _explicit_replace_request("replace 'hi\" with 'hello' in hello.txt") is None


def test_quoted_replace_in_a_marked_folder_lands_after_the_approval(profile_env, marked):
    (marked / "hello.txt").write_bytes(b"hi there\n")
    agent = make_agent()
    first, resumed = _approve_and_resume(agent, "replace 'hi' with 'hello' in hello.txt", "captest-b8-replace", marked)
    assert "requires approval" in first and str(marked / "hello.txt") in first
    assert (marked / "hello.txt").read_bytes() == b"hello there\n", resumed


def test_quoted_replace_keeps_mixed_line_endings_and_trailing_spaces(profile_env, marked):
    (marked / "mixed.txt").write_bytes(b"one\r\ntwo\nthree   \r\nfour\n")
    agent = make_agent()
    _approve_and_resume(agent, "replace 'two' with 'TWO' in mixed.txt", "captest-b8-mixed", marked)
    assert (marked / "mixed.txt").read_bytes() == b"one\r\nTWO\nthree   \r\nfour\n"


# --- B8: the undo -----------------------------------------------------------------------------------------------


def test_undo_after_a_workspace_edit_is_not_claimed_by_the_profile(profile_env, marked):
    (marked / "hello.txt").write_bytes(b"hi there\n")
    agent = make_agent()
    session = "captest-b8-undo"
    _approve_and_resume(agent, "replace 'hi' with 'hello' in hello.txt", session, marked)
    assert (marked / "hello.txt").read_bytes() == b"hello there\n"
    reply, _ = _chat(agent, "undo the last change", session, marked)
    assert "profile change" not in reply, reply


def test_undo_words_seat_the_workspace_rollback():
    from core.tool_demand_signals import resolve_demand_signals

    for words in ("undo the last change", "please undo my last edit", "revert the last change"):
        assert "workspace.rollback_last_change" in resolve_demand_signals(words).explicit_intents, words


def _record_mutation(session: str, workspace: Path) -> None:
    from core.execution.artifacts import record_workspace_mutation

    record_workspace_mutation(
        session_id=session,
        workspace_root=workspace,
        intent="workspace.replace_in_file",
        changes=[{"path": "hello.txt", "existed_before": True, "existed_after": True,
                  "before_text": "hi there\n", "after_text": "hello there\n"}],
    )


def test_profile_owns_an_undo_only_when_its_change_is_the_newest(profile_env, tmp_path_factory):
    from core import operator_profile as profile
    from core.operator_profile_turn import profile_owns_undo

    workspace = tmp_path_factory.mktemp("ws").resolve()
    owner, session = "owner_local", "captest-b8-owner"
    assert profile_owns_undo(owner, session) is False  # nothing in the profile to undo
    profile.remember(owner, "preferred_name", "Alex", scope="global", session_id=session, turn_id="t1",
                     origin="explicit", confidence=1.0, actor="chat", reason="explicit: test")
    assert profile_owns_undo(owner, session) is True
    _record_mutation(session, workspace)
    assert profile_owns_undo(owner, session) is False  # the workspace change is newer
    assert profile_owns_undo(owner, "another-chat") is True  # another chat's ledger is not this one
    item = profile.list_items(owner, session_id=session)[0]
    assert profile.forget_item(item.item_id, actor="chat", reason="forget: test").kind == "forgotten"
    assert profile_owns_undo(owner, session) is True  # the profile change is newer again


# --- B12: the move ----------------------------------------------------------------------------------------------


def _move(target: str | None, destination: str | None, raw_text: str):
    from core.operator.models import OperatorActionIntent

    return OperatorActionIntent(kind="move_path", target_path=target, destination_path=destination, raw_text=raw_text)


def test_a_move_the_handler_reads_nothing_from_is_answered_by_the_handler(profile_env, marked):
    from core.local_operator_actions import dispatch_operator_action, windows_write_refusal

    source = marked / "hello.txt"
    source.write_bytes(b"hi\n")
    intent = _move(str(source), None, f"move {source} into it")
    assert windows_write_refusal(intent) is None
    result = dispatch_operator_action(intent, task_id="captest-b12", session_id="captest-b12")
    assert result.status == "invalid_request", result.response_text
    assert "names no destination_path" not in result.response_text
    assert source.read_bytes() == b"hi\n"
    assert sorted(p.name for p in marked.iterdir()) == ["hello.txt"]


def test_every_move_the_handler_reads_is_still_gated(profile_env, marked, tmp_path_factory):
    from core.local_operator_actions import windows_write_refusal

    outside = tmp_path_factory.mktemp("outside").resolve()
    source = marked / "hello.txt"
    source.write_bytes(b"hi\n")
    (marked / "sub").mkdir()
    # Both inside the marked folder: the gate lets the handler preview it.
    assert windows_write_refusal(_move(str(source), str(marked / "sub"), f'move "{source}" to "{marked / "sub"}"')) is None
    # Into a folder outside the marked folders: refused.
    assert windows_write_refusal(_move(str(source), str(outside), f'move "{source}" to "{outside}"'))
    # An archive request moves into VOOL's own archive folder, outside the marked folders: refused.
    assert windows_write_refusal(_move(str(source), None, f'archive "{source}"'))
    # A relative source cannot be placed by the gate: refused.
    assert windows_write_refusal(_move("hello.txt", str(marked / "sub"), 'move "hello.txt" to "sub"'))
