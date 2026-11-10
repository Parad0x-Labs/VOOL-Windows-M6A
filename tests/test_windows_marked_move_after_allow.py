"""Windows: a move the model asks for between two marked folders runs once after the owner's Allow.

Captest round 2 (build a22771c2, clean Win11 VM): with two marked folders, the owner asked VOOL to
move a file from marked folder A into marked folder B. The approval card appeared; after Allow the
run card stayed on "Resuming pending tool…" and nothing moved. A move to an unmarked folder was
refused before any card, as it should be.

"Resuming pending tool" is said only by the tool loop when it replays the call the owner approved
(core/agent_runtime/research_tool_loop_facade.py), so the move was a call the model chose, not the
deterministic marked-folder lane (core/windows_marked_file_ops.py), which reads only "move <file> to
<folder>" with both paths written out. The model's move tool is `operator.move_path`. Its handler
answers a call with no stored preview by storing one and asking for a second, typed approval
("Reply with: approve move <id>"); the tool executor builds every model call with
`approval_requested=False`, so after the card's Allow the replayed call made a preview again and
ended as a `tool_preview` carrying no approval id. The page shows no card for that and waits.

What this file pins, through the production chat door (`dispatch_post`, a real `VoolAgent`, the model's
tool choice scripted, the chat unbound so its workspace is the Desktop):

- after the card's Allow, the approved `operator.move_path` moves the file once, from marked folder A
  into marked folder B, with exactly the approved paths, with no second card or typed approval;
- a replay of the same token moves nothing more, and Deny moves nothing;
- a move to an unmarked folder is still refused before any card;
- the approval never runs an earlier stored preview: one the model names by id is refused in plain
  words with nothing moved, and one the chat still holds is left as it is;
- on Windows, once the broker is wired, the approved move goes through the broker's `rename`, and
  through nothing else;
- off Windows nothing changes: the approved call still ends as a preview and nothing moves.

Windows is simulated as the other Windows suites do: the write gate's `_IS_WINDOWS`, the known
folders' `_IS_WINDOWS` (with GetDriveTypeW answered "local" for the drive that holds the test folders),
the job runner's `_is_windows_platform` seam (the seam an exact approval's spend reads,
core/execution_gate.py `operator_approval_spend_applies`) and `network_guard._IS_WINDOWS`. The marked
folders are listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES, as tests/_windows_disposable_tmp.py lists a
test's own folder. `windows_path_policy._IS_WINDOWS`, `write_root_honesty._ON_WINDOWS` and
`windows_marked_file_ops._ON_WINDOWS` judge a path by its Windows spelling (a drive letter): on Linux
a test folder has none, so they are left to the host and are on only on a Windows host, where the
whole file runs with real `X:\\` paths.
"""
from __future__ import annotations

import ntpath
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from core import windows_known_folders, windows_path_policy
from core import windows_write_gate as gate
from tests._windows_disposable_tmp import running_on_windows
from tests.operator_profile_rig import body_json, in_request_thread, make_agent, profile_env_generator


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


def _windows_seams(monkeypatch, *, job_runner_windows: bool = True) -> None:
    from core import windows_marked_file_ops
    from core.agent_runtime import write_root_honesty
    from sandbox import job_runner, network_guard

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", True)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: job_runner_windows)
    monkeypatch.setattr(network_guard, "_IS_WINDOWS", True)
    if running_on_windows():
        # Spelling rules for `X:\\` paths; on Linux a test folder has no drive letter to judge.
        monkeypatch.setattr(windows_path_policy, "_IS_WINDOWS", True)
        monkeypatch.setattr(write_root_honesty, "_ON_WINDOWS", True)
        monkeypatch.setattr(windows_marked_file_ops, "_ON_WINDOWS", True)


@pytest.fixture()
def machine(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop, two marked folders with spaces and `Ä` in the user's home outside
    Desktop, Documents and Downloads (as on the VM: `C:\\Users\\VOOLT\\VOOL Test\\…`), an unmarked folder
    next to them, and the system temp folder elsewhere (on a Windows runner `%TEMP%` does not hold the
    test folders, which the move handler's scope would otherwise accept as temp)."""
    root = tmp_path_factory.mktemp("machine").resolve()
    home = root / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = root / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    first = home / "VOOL Test" / "Marked A Ä"
    second = home / "VOOL Test" / "Marked B"
    unmarked = home / "VOOL Test" / "Other folder"
    for folder in (first, second, unmarked):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    import tempfile
    (root / "SystemTemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root / "SystemTemp"))
    local_drive = ntpath.splitdrive(str(root))[0].upper()
    monkeypatch.setattr(windows_known_folders, "_drive_is_remote",
                        lambda drive: False if str(drive or "").upper().rstrip("\\/") == local_drive else None)
    _windows_seams(monkeypatch)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(first), str(second)]))
    return {"root": root, "home": home, "workspace": workspace, "first": first, "second": second, "unmarked": unmarked}


class _Model:
    """The model's tool choice, scripted: `calls` in order, then a closing reply, for every turn."""

    def __init__(self, *calls: dict[str, Any]) -> None:
        self.calls = list(calls)
        self.asked = 0

    def __call__(self, **_kwargs: Any):
        from core.memory_first_router import ModelExecutionDecision

        self.asked += 1
        output = self.calls.pop(0) if self.calls else {"intent": "respond.direct", "arguments": {"message": "Done."}}
        return ModelExecutionDecision(
            source="provider_execution", task_hash=f"t{self.asked}", provider_id="ollama-local:test",
            provider_name="ollama-local", model_name="test", structured_output=output,
            confidence=0.8, trust_score=0.84, used_model=True, validation_state="valid",
        )


def _move_call(source: Path, destination: Path, **extra: Any) -> dict[str, Any]:
    return {"intent": "operator.move_path",
            "arguments": {"source_path": str(source), "destination_path": str(destination), **extra}}


def _agent(monkeypatch, model: _Model):
    agent = make_agent()
    monkeypatch.setattr(agent.memory_router, "resolve_tool_intent", model)
    return agent


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


def _approvals(turn_id: str, status: str = "pending") -> list[dict]:
    from core.mode_permission_policy import _APPROVALS

    return [a for a in _APPROVALS.values() if a.get("status") == status and a.get("task_id") == turn_id]


def _token_status(token: str) -> str:
    from core.mode_permission_policy import _APPROVALS

    return str((_APPROVALS.get(token) or {}).get("status") or "")


def _ask(agent, text: str, session: str, workspace: Path) -> tuple[str, str, str]:
    """The turn asks; returns (reply, turn id, the card's token)."""
    reply, turn_id = _chat(agent, text, session, workspace)
    pending = _approvals(turn_id)
    assert len(pending) == 1, reply
    assert pending[0].get("intent") == "operator.move_path", pending[0]
    return reply, turn_id, str(pending[0].get("approval_id"))


def _allow_and_resend(agent, text: str, session: str, workspace: Path, turn_id: str, token: str) -> str:
    """The chat page's flow: the owner allows the card, the same turn is resent with its token."""
    from core.mode_permission_policy import resolve_approval

    assert resolve_approval(token, decision="allow") is not None
    resumed, _ = _chat(agent, text, session, workspace, turn_id=turn_id, token=token)
    return resumed


def _pending_previews(session_hint: str = "") -> list[dict]:
    from storage.db import get_connection

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM operator_action_requests WHERE action_kind = 'move_path' AND status = 'pending_approval'"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows if session_hint in str(dict(row).get("session_id") or "")]


#: The owner's words name no path. Words with a full drive-letter path are read on Windows by the operator
#: lane's own parser at the front door (never on Linux, where `/tmp/...` is not read there), and never
#: reach the model; the model's call carries the paths.
TEXT = "move hello.txt from the Marked A folder to the Marked B folder"


# --- the served chat: Allow moves the file --------------------------------------------------------------


def test_after_allow_the_model_move_between_two_marked_folders_lands(profile_env, machine, monkeypatch) -> None:
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"one\r\ntwo\n")
    model = _Model(_move_call(source, machine["second"]))
    agent = _agent(monkeypatch, model)
    text = TEXT.format(a=machine["first"], b=machine["second"])
    first, turn_id, token = _ask(agent, text, "r2-move", machine["workspace"])
    assert "requires approval" in first, first
    assert source.exists()  # nothing moved before the owner allowed it
    resumed = _allow_and_resend(agent, text, "r2-move", machine["workspace"], turn_id, token)
    assert not source.exists(), resumed
    assert (machine["second"] / "hello.txt").read_bytes() == b"one\r\ntwo\n"
    assert "wasn't able" not in resumed and "approve move" not in resumed, resumed
    assert _approvals(turn_id) == []  # no second card is waiting
    assert _token_status(token) == "consumed"
    assert _pending_previews() == []  # and no typed second approval either
    assert list(machine["workspace"].iterdir()) == []  # the Desktop is untouched
    assert sorted(p.name for p in machine["unmarked"].iterdir()) == []


def test_after_allow_a_move_naming_the_destination_file_lands_there(profile_env, machine, monkeypatch) -> None:
    """The model may name the folder or the file it lands as; the handler moves into the folder named."""
    source = machine["first"] / "notes Ä.txt"
    source.write_bytes(b"hi\n")
    sub = machine["second"] / "kept"
    sub.mkdir()
    agent = _agent(monkeypatch, _Model(_move_call(source, sub)))
    text = "please move my notes file into the kept folder"  # names no path
    _first, turn_id, token = _ask(agent, text, "r2-move-sub", machine["workspace"])
    resumed = _allow_and_resend(agent, text, "r2-move-sub", machine["workspace"], turn_id, token)
    assert not source.exists() and (sub / "notes Ä.txt").read_bytes() == b"hi\n", resumed


def test_the_approved_move_runs_once(profile_env, machine, monkeypatch) -> None:
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    model = _Model(_move_call(source, machine["second"]), _move_call(source, machine["second"]))
    agent = _agent(monkeypatch, model)
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-once", machine["workspace"])
    _allow_and_resend(agent, text, "r2-once", machine["workspace"], turn_id, token)
    assert (machine["second"] / "hello.txt").exists()
    # The same file appears again at the source; the spent token is sent once more.
    source.write_bytes(b"again\n")
    (machine["second"] / "hello.txt").unlink()
    again, _ = _chat(agent, text, "r2-once", machine["workspace"], turn_id=turn_id, token=token)
    assert source.read_bytes() == b"again\n", again
    assert not (machine["second"] / "hello.txt").exists()


def test_deny_moves_nothing(profile_env, machine, monkeypatch) -> None:
    from core.mode_permission_policy import resolve_approval

    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"]), _move_call(source, machine["second"])))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-deny", machine["workspace"])
    assert resolve_approval(token, decision="deny") is not None
    _chat(agent, text, "r2-deny", machine["workspace"], turn_id=turn_id, token=token)
    assert source.exists() and not (machine["second"] / "hello.txt").exists()
    assert _pending_previews() == []


# --- refusals stay before approval -----------------------------------------------------------------------


@pytest.mark.parametrize("where", ["unmarked", "documents"])
def test_a_move_out_of_the_marked_folders_is_refused_before_any_card(profile_env, machine, monkeypatch, where) -> None:
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    destination = machine["unmarked"] if where == "unmarked" else machine["home"] / "Documents"
    agent = _agent(monkeypatch, _Model(_move_call(source, destination)))
    reply, turn_id = _chat(agent, f"move hello.txt from the Marked A folder to {where}", f"r2-out-{where}",
                           machine["workspace"])
    assert _approvals(turn_id) == [], reply
    assert source.exists() and not (destination / "hello.txt").exists()
    assert _pending_previews() == []


def test_plan_mode_still_refuses_the_move_with_no_card(profile_env, machine, monkeypatch) -> None:
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"])))
    reply, turn_id = _chat(agent, TEXT.format(a=machine["first"], b=machine["second"]), "r2-plan",
                           machine["workspace"], mode="plan")
    assert _approvals(turn_id) == [], reply
    assert source.exists() and not (machine["second"] / "hello.txt").exists()


def test_a_move_between_marked_folders_the_move_handler_does_not_take_is_refused_before_any_card(
        profile_env, machine, monkeypatch) -> None:
    """Two marked folders outside the user's home (a marked `D:\\Work`): the write gate allows them, but
    the move handler takes a source and destination only under the home, the temp folder or VOOL's
    data folder. Its own refusal comes before any card, not after the owner's Allow."""
    work = machine["root"] / "Work"
    left, right = work / "Marked C", work / "Marked D"
    for folder in (left, right):
        folder.mkdir(parents=True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(machine["first"]), str(machine["second"]), str(left), str(right)]))
    source = left / "hello.txt"
    source.write_bytes(b"hi\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, right)))
    reply, turn_id = _chat(agent, "move hello.txt from the Marked C folder to the Marked D folder", "r2-out-of-home",
                           machine["workspace"])
    assert _approvals(turn_id) == [], reply
    assert source.exists() and not (right / "hello.txt").exists()
    assert _pending_previews() == []


# --- an approval runs only its own call ------------------------------------------------------------------


def _store_preview(session_id: str, source: Path, folder: Path, action_id: str = "") -> str:
    """An earlier move preview this chat holds, as the operator lane stores one."""
    import json

    from storage.db import get_connection

    action_id = action_id or f"mv-{uuid.uuid4().hex[:8]}"
    scope = {"source_path": str(source), "destination_dir": str(folder), "destination_path": str(folder / source.name)}
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO operator_action_requests (action_id, session_id, task_id, action_kind, scope_json, result_json,"
            " status, created_at, updated_at, executed_at) VALUES (?, ?, 'earlier-task', 'move_path', ?, '{}',"
            " 'pending_approval', '2026-11-10T00:00:00+00:00', '2026-11-10T00:00:00+00:00', NULL)",
            (action_id, session_id, json.dumps(scope, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()
    return action_id


def _hold_earlier_preview(monkeypatch, earlier: Path, folder: Path, action_id: str = "") -> None:
    """The chat holds an earlier preview from its first lookup on, under whatever session id the lane uses."""
    from core import local_operator_actions as ops

    real_load = ops._load_pending_action
    stored: set[str] = set()

    def _load(*, session_id, action_kind, action_id=None):
        if session_id and session_id not in stored:
            stored.add(session_id)
            _store_preview(session_id, earlier, folder, action_id=held_id)
        return real_load(session_id=session_id, action_kind=action_kind, action_id=action_id)

    held_id = action_id
    monkeypatch.setattr(ops, "_load_pending_action", _load)


def test_the_approval_never_runs_an_earlier_preview_the_model_names_by_id(profile_env, machine, monkeypatch) -> None:
    earlier = machine["first"] / "earlier.txt"
    earlier.write_bytes(b"earlier\n")
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    _hold_earlier_preview(monkeypatch, earlier, machine["second"], action_id="mv-earlier")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"], action_id="mv-earlier")))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-earlier-id", machine["workspace"])
    resumed = _allow_and_resend(agent, text, "r2-earlier-id", machine["workspace"], turn_id, token)
    assert earlier.read_bytes() == b"earlier\n" and not (machine["second"] / "earlier.txt").exists(), resumed
    assert source.exists() and not (machine["second"] / "hello.txt").exists(), resumed
    assert "Nothing was moved" in resumed, resumed


def test_an_earlier_preview_the_chat_holds_is_neither_run_nor_moved_by_the_approval(profile_env, machine, monkeypatch) -> None:
    """The chat holds an earlier move preview; the approved call moves its own file, never that one."""
    earlier = machine["first"] / "earlier.txt"
    earlier.write_bytes(b"earlier\n")
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    _hold_earlier_preview(monkeypatch, earlier, machine["second"])
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"])))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-earlier-held", machine["workspace"])
    resumed = _allow_and_resend(agent, text, "r2-earlier-held", machine["workspace"], turn_id, token)
    assert earlier.read_bytes() == b"earlier\n" and not (machine["second"] / "earlier.txt").exists(), resumed
    assert not source.exists() and (machine["second"] / "hello.txt").read_bytes() == b"hi\n", resumed


# --- the broker ------------------------------------------------------------------------------------------


def test_once_the_broker_is_wired_the_approved_move_goes_through_its_rename(profile_env, machine, monkeypatch) -> None:
    import shutil

    from core.windows_broker import writes

    renamed: list[tuple[str, str]] = []
    folders: list[str] = []

    def _rename(src: Path, dst: Path) -> None:
        renamed.append((str(src), str(dst)))
        os.replace(src, dst)

    def _by_path(*_args, **_kwargs):
        raise AssertionError("an approved move on Windows went by path, not through the broker")

    monkeypatch.setattr(writes, "routed", lambda: True)
    monkeypatch.setattr(writes, "rename", _rename)
    monkeypatch.setattr(writes, "ensure_directory", lambda folder: folders.append(str(folder)) or ())
    monkeypatch.setattr(shutil, "move", _by_path)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"])))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-broker", machine["workspace"])
    resumed = _allow_and_resend(agent, text, "r2-broker", machine["workspace"], turn_id, token)
    assert renamed == [(str(source), str(machine["second"] / "hello.txt"))], resumed
    assert folders == [str(machine["second"])]
    assert (machine["second"] / "hello.txt").read_bytes() == b"hi\n"


# --- off Windows -----------------------------------------------------------------------------------------


def test_off_windows_the_approved_call_is_left_as_it_was(profile_env, machine, monkeypatch) -> None:
    """The spend of an exact approval applies on Windows only for 0.7.1 (Security Architect, 9 Nov): with
    the job runner's seam off, the replayed call still stores a preview and nothing moves, as on base."""
    _windows_seams(monkeypatch, job_runner_windows=False)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"])))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-off", machine["workspace"])
    _allow_and_resend(agent, text, "r2-off", machine["workspace"], turn_id, token)
    assert source.exists() and not (machine["second"] / "hello.txt").exists()
    assert len(_pending_previews()) == 1  # the typed second approval it asks for, as before


# --- the executor's own seam -----------------------------------------------------------------------------


def test_only_an_exactly_approved_operator_move_is_run_from_its_fresh_preview(profile_env, machine) -> None:
    """At the operator tool itself: without the approved-call scope the call stores a preview, as before;
    inside the scope for another intent, the same; inside the scope for this call it moves once."""
    from core.execution.operator_tools import execute_operator_tool
    from core.execution_gate import operator_approved_call

    source = machine["first"] / "hello.txt"
    arguments = {"source_path": str(source), "destination_path": str(machine["second"])}
    source.write_bytes(b"hi\n")

    plain = execute_operator_tool("operator.move_path", arguments, task_id="t-1", session_id="seam-1")
    assert plain.mode == "tool_preview" and source.exists()
    with operator_approved_call("sandbox.run_command"):
        other = execute_operator_tool("operator.move_path", arguments, task_id="t-2", session_id="seam-2")
    assert other.mode == "tool_preview" and source.exists()
    with operator_approved_call("operator.move_path"):
        moved = execute_operator_tool("operator.move_path", arguments, task_id="t-3", session_id="seam-3")
        source.write_bytes(b"again\n")
        second = execute_operator_tool("operator.move_path",
                                       {"source_path": str(source), "destination_path": str(machine["unmarked"])},
                                       task_id="t-4", session_id="seam-4")
    assert moved.mode == "tool_executed" and moved.status == "executed", moved
    assert (machine["second"] / "hello.txt").read_bytes() == b"hi\n"
    assert second.mode != "tool_executed" and source.read_bytes() == b"again\n"  # the scope is spent once
    assert not (machine["unmarked"] / "hello.txt").exists()



# --- an approved apply that comes back without a move closes its preview ----------------------------------
# Security Architect condition on ffed811b/ccaa7c74: an approved apply that ends blocked or failed (the
# broker's rename refused, a conflict at the apply, the stored path refused) used to leave its preview
# `pending_approval`. Served, the next move still got its own preview (the executor gives each model call
# its receipt key as `action_id`, so the handler never fell back to the chat's newest preview), but a
# dispatch with no such id answered every later approved move in that chat "This chat holds another move
# preview" from that stale row, and a typed "approve move <id>" could still run it later.


def _preview_rows() -> list[dict]:
    from storage.db import get_connection

    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM operator_action_requests WHERE action_kind = 'move_path'").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _broker_refusing_once(monkeypatch) -> list[str]:
    """A wired broker whose first rename is refused (`changed_since_check`), then moves by `os.replace`."""
    from core.windows_broker import writes

    refused: list[str] = []

    def _rename(src: Path, dst: Path) -> None:
        if not refused:
            refused.append(str(src))
            raise writes.Refused("changed_since_check", "the file changed since VOOL checked it")
        os.replace(src, dst)

    monkeypatch.setattr(writes, "routed", lambda: True)
    monkeypatch.setattr(writes, "rename", _rename)
    monkeypatch.setattr(writes, "ensure_directory", lambda folder: ())
    return refused


def _approved_operator_move(source: Path, folder: Path, session_id: str):
    from core.execution.operator_tools import execute_operator_tool
    from core.execution_gate import operator_approved_call

    with operator_approved_call("operator.move_path"):
        return execute_operator_tool("operator.move_path", {"source_path": str(source), "destination_path": str(folder)},
                                     task_id="t-apply", session_id=session_id)


def test_a_failed_approved_apply_leaves_no_preview_pending(profile_env, machine, monkeypatch) -> None:
    refused = _broker_refusing_once(monkeypatch)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    failed = _approved_operator_move(source, machine["second"], "apply-fail")
    assert refused and failed.status == "execution_failed" and source.exists(), failed
    assert [row["status"] for row in _preview_rows()] == ["cancelled"]


def test_a_second_approved_move_after_a_failed_apply_does_not_loop(profile_env, machine, monkeypatch) -> None:
    _broker_refusing_once(monkeypatch)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    other = machine["first"] / "other.txt"
    other.write_bytes(b"other\n")
    _approved_operator_move(source, machine["second"], "apply-loop")
    second = _approved_operator_move(other, machine["second"], "apply-loop")
    assert "holds another move preview" not in second.response_text, second.response_text
    assert second.status == "executed" and (machine["second"] / "other.txt").read_bytes() == b"other\n"
    assert source.exists() and not (machine["second"] / "hello.txt").exists()  # the failed one is not re-run


def test_a_typed_approval_of_the_failed_preview_runs_nothing(profile_env, machine, monkeypatch) -> None:
    from core.local_operator_actions import OperatorActionIntent, dispatch_operator_action

    _broker_refusing_once(monkeypatch)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    _approved_operator_move(source, machine["second"], "apply-typed")
    (action_id,) = [row["action_id"] for row in _preview_rows()]
    typed = dispatch_operator_action(
        OperatorActionIntent(kind="move_path", approval_requested=True, action_id=action_id,
                             raw_text=f"approve move {action_id}"),
        task_id="t-typed", session_id="apply-typed",
    )
    assert not typed.ok and source.exists() and not (machine["second"] / "hello.txt").exists(), typed


def test_an_outcome_unknown_apply_is_recorded_unproven_not_pending(profile_env, machine, monkeypatch) -> None:
    from core.windows_broker import writes

    def _lost(src: Path, dst: Path) -> None:
        raise writes.OutcomeUnknown(reason="broker_reply_lost", detail="probe")

    monkeypatch.setattr(writes, "routed", lambda: True)
    monkeypatch.setattr(writes, "rename", _lost)
    monkeypatch.setattr(writes, "ensure_directory", lambda folder: ())
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    with pytest.raises(writes.OutcomeUnknown):
        _approved_operator_move(source, machine["second"], "apply-unknown")
    assert [row["status"] for row in _preview_rows()] == ["outcome_unproven"]


def test_served_a_second_move_after_a_failed_approved_apply_lands(profile_env, machine, monkeypatch) -> None:
    _broker_refusing_once(monkeypatch)
    source = machine["first"] / "hello.txt"
    source.write_bytes(b"hi\n")
    other = machine["first"] / "other.txt"
    other.write_bytes(b"other\n")
    agent = _agent(monkeypatch, _Model(_move_call(source, machine["second"]), _move_call(other, machine["second"])))
    text = TEXT.format(a=machine["first"], b=machine["second"])
    _first, turn_id, token = _ask(agent, text, "r2-after-fail", machine["workspace"])
    failed = _allow_and_resend(agent, text, "r2-after-fail", machine["workspace"], turn_id, token)
    assert source.exists() and "couldn't move" in failed, failed
    assert _pending_previews() == []
    text2 = TEXT.replace("hello.txt", "other.txt").format(a=machine["first"], b=machine["second"])
    _second, turn2, token2 = _ask(agent, text2, "r2-after-fail", machine["workspace"])
    landed = _allow_and_resend(agent, text2, "r2-after-fail", machine["workspace"], turn2, token2)
    assert "holds another move preview" not in landed, landed
    assert not other.exists() and (machine["second"] / "other.txt").read_bytes() == b"other\n"
    assert source.exists()
