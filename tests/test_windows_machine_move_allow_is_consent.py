"""Windows: the owner's Allow on the card is the consent for a `machine.move_path`; no OS prompt follows.

Captest round 4, R4-1b (build 5f97b517, Win11 VM, CPU-only). "move C:\\Users\\VOOLT\\VOOL Test\\Marked A\\notes.txt
to C:\\Users\\VOOLT\\VOOL Test\\Marked B" raised the card "Run machine.move_path · Affects: No specific path
reported". After Allow nothing completed for 15 minutes and nothing moved: the approved call reached
`machine_file_ops.move_path`, which then asked a second, live OS confirmation (`os_consent_gate`, Windows
Hello or the CredUI credential dialog with no parent window, from a backend started without a window).
That prompt never reached the owner and the turn waited on it.

On Windows (the job runner's `_is_windows_platform` seam, the one an exact approval's spend reads):
- the call that spent the owner's live approval token claims the authorized boundary's
  `operator_approved_call` scope once, and the move runs with that Allow as its consent; no OS prompt;
- any other call (Auto, a direct dispatch, an allow that is not the live token) is refused plainly
  before anything moves, and the OS prompt is never opened;
- the card names the source and the final destination, and a call it cannot name both for is refused
  before the card;
- everything else the move checks (write gate, scope, conflict, broker rename) still runs.
Off Windows the OS prompt is asked exactly as before.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from core import windows_known_folders
from core import windows_write_gate as gate
from tests.operator_profile_rig import body_json, in_request_thread, make_agent, profile_env_generator

REFUSAL = "Moving files needs your Allow on the card; nothing was moved."


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    yield from profile_env_generator(tmp_path, monkeypatch)


def _seams(monkeypatch, *, windows: bool) -> None:
    from sandbox import job_runner

    monkeypatch.setattr(gate, "_IS_WINDOWS", windows)
    monkeypatch.setattr(windows_known_folders, "_IS_WINDOWS", windows)
    monkeypatch.setattr(job_runner, "_is_windows_platform", lambda: windows)


@pytest.fixture()
def machine(tmp_path_factory, monkeypatch) -> dict[str, Path]:
    """The chat's (unmarked) Desktop, two marked folders with spaces in the user's home (as on the VM),
    and an unmarked folder next to them."""
    root = tmp_path_factory.mktemp("machine").resolve()
    home = root / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = root / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    first = home / "VOOL Test" / "Marked A"
    second = home / "VOOL Test" / "Marked B"
    unmarked = home / "VOOL Test" / "Other folder"
    for folder in (first, second, unmarked):
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _seams(monkeypatch, windows=True)
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(first), str(second)]))
    return {"home": home, "workspace": workspace, "first": first, "second": second, "unmarked": unmarked}


@pytest.fixture()
def consent(monkeypatch):
    """The OS prompt. Every call is recorded (and answered no): on Windows a test expects none.
    `move_path` swallows an exception from the prompt, so the record is what the tests assert on."""
    from core import os_consent_gate

    asked: list[str] = []

    def _record(reason: str) -> bool:
        asked.append(reason)
        return False

    monkeypatch.setattr(os_consent_gate, "_TEST_OVERRIDE", _record)
    return asked


@pytest.fixture()
def consent_yes(monkeypatch):
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


def _ask(agent, text: str, session: str, workspace: Path) -> tuple[str, str, dict]:
    reply, turn_id = _chat(agent, text, session, workspace)
    pending = _pending(turn_id)
    assert len(pending) == 1, reply
    assert pending[0].get("intent") == "machine.move_path", pending[0]
    return reply, turn_id, pending[0]


def _owner_context(folder: Path) -> dict:
    from core.request_trust import OWNER_LOCAL_KEY

    return {OWNER_LOCAL_KEY: True, "session_id": "unit-move", "workspace": str(folder),
            "workspace_root": str(folder)}


def _move_text(source: Path, destination: Path) -> str:
    return f"move {source} to {destination}"


# --- the served chat ---------------------------------------------------------------------------------------


def test_an_exact_allow_moves_once_with_no_os_prompt(profile_env, machine, consent) -> None:
    from core.mode_permission_policy import resolve_approval

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"one\r\ntwo\n")
    agent = make_agent()
    text = _move_text(source, machine["second"])
    _first, turn_id, card = _ask(agent, text, "r41b-allow", machine["workspace"])
    assert source.exists()  # nothing moved before the Allow
    assert resolve_approval(str(card["approval_id"]), decision="allow") is not None
    resumed, _ = _chat(agent, text, "r41b-allow", machine["workspace"], turn_id=turn_id, token=str(card["approval_id"]))
    assert not source.exists(), resumed
    assert (machine["second"] / "notes.txt").read_bytes() == b"one\r\ntwo\n"
    assert consent == []  # the Allow was the consent: no OS prompt
    assert _pending(turn_id) == []
    assert list(machine["workspace"].iterdir()) == []


def test_a_replay_of_the_spent_approval_moves_nothing(profile_env, machine, consent) -> None:
    from core.mode_permission_policy import resolve_approval

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    agent = make_agent()
    text = _move_text(source, machine["second"])
    _first, turn_id, card = _ask(agent, text, "r41b-replay", machine["workspace"])
    token = str(card["approval_id"])
    assert resolve_approval(token, decision="allow") is not None
    _chat(agent, text, "r41b-replay", machine["workspace"], turn_id=turn_id, token=token)
    assert (machine["second"] / "notes.txt").exists()
    # The same file is back at the source; the spent token is sent again.
    source.write_bytes(b"again\n")
    (machine["second"] / "notes.txt").unlink()
    again, _ = _chat(agent, text, "r41b-replay", machine["workspace"], turn_id=turn_id, token=token)
    assert source.read_bytes() == b"again\n", again
    assert not (machine["second"] / "notes.txt").exists()
    assert consent == []


def test_deny_moves_nothing(profile_env, machine, consent) -> None:
    from core.mode_permission_policy import resolve_approval

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    agent = make_agent()
    text = _move_text(source, machine["second"])
    _first, turn_id, card = _ask(agent, text, "r41b-deny", machine["workspace"])
    token = str(card["approval_id"])
    assert resolve_approval(token, decision="deny") is not None
    _chat(agent, text, "r41b-deny", machine["workspace"], turn_id=turn_id, token=token)
    assert source.exists() and not (machine["second"] / "notes.txt").exists()
    assert consent == []


@pytest.mark.parametrize("where", ["unmarked", "documents"])
def test_a_move_outside_the_marked_folders_is_refused_before_the_card(profile_env, machine, consent, where) -> None:
    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    destination = machine["unmarked"] if where == "unmarked" else machine["home"] / "Documents"
    agent = make_agent()
    reply, turn_id = _chat(agent, _move_text(source, destination), f"r41b-out-{where}", machine["workspace"])
    assert "Nothing was moved." in reply, reply
    assert _pending(turn_id) == [] and consent == []
    assert source.exists() and not (destination / "notes.txt").exists()


def test_the_card_names_the_source_and_the_final_destination(profile_env, machine, consent) -> None:
    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    agent = make_agent()
    _first, _turn_id, card = _ask(agent, _move_text(source, machine["second"]), "r41b-card", machine["workspace"])
    resources = list(card.get("affected_resources") or [])
    assert str(source) in resources, resources
    assert str(machine["second"] / "notes.txt") in resources, resources  # the final path, not the folder


# --- the tool's own seam: no exact approval ----------------------------------------------------------------


def _allow(*, exact: bool, token_spent: bool):
    from core.mode_permission_policy import OperatingMode, PermissionDecision, PermissionEffect

    return PermissionDecision(
        effect=PermissionEffect.ALLOW, mode=OperatingMode.AUTO, actions=(), reason="test allow",
        exact_approval=exact, **({"token_spent": True} if token_spent else {}),
    )


@pytest.mark.parametrize(
    "decision",
    [
        pytest.param(lambda: _allow(exact=False, token_spent=False), id="auto-allow"),
        pytest.param(lambda: _allow(exact=True, token_spent=False), id="task-grant"),
    ],
)
def test_an_allow_that_is_not_the_live_token_is_refused_with_no_os_prompt(machine, consent, decision) -> None:
    from core.authorized_tool_execution import execute_authorized_runtime_tool

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    destination = machine["second"] / "notes.txt"
    result = execute_authorized_runtime_tool(
        "machine.move_path", {"source": str(source), "destination": str(destination)},
        source_context=_owner_context(machine["first"]), permission_decision=decision(),
    )
    assert result is not None and not result.ok
    assert REFUSAL in str(result.response_text), result.response_text
    assert source.exists() and not destination.exists()
    assert consent == []


def test_a_direct_dispatch_on_windows_is_refused_with_no_os_prompt(machine, consent) -> None:
    from core.runtime_execution_tools import execute_runtime_tool

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    destination = machine["second"] / "notes.txt"
    result = execute_runtime_tool(
        "machine.move_path", {"source": str(source), "destination": str(destination)},
        source_context=_owner_context(machine["first"]),
    )
    assert result is not None and not result.ok
    assert REFUSAL in str(result.response_text), result.response_text
    assert source.exists() and not destination.exists()
    assert consent == []


def test_auto_mode_on_windows_moves_nothing_and_opens_no_os_prompt(profile_env, machine, consent) -> None:
    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    agent = make_agent()
    reply, turn_id = _chat(agent, _move_text(source, machine["second"]), "r41b-auto", machine["workspace"],
                           mode="auto")
    assert consent == [], reply
    # Auto either asks on the card or refuses plainly; it never moves without the owner's Allow.
    assert source.exists() and not (machine["second"] / "notes.txt").exists(), reply
    if not _pending(turn_id):
        assert REFUSAL in reply, reply


@pytest.mark.parametrize("missing", ["source", "destination"])
def test_a_move_the_card_cannot_name_both_paths_for_is_refused_before_the_card(machine, consent, missing) -> None:
    from core.authorized_tool_execution import execute_authorized_runtime_tool
    from core.mode_permission_policy import _APPROVALS

    source = machine["first"] / "notes.txt"
    source.write_bytes(b"hi\n")
    arguments = {"source": str(source), "destination": str(machine["second"] / "notes.txt")}
    arguments[missing] = ""
    before = set(_APPROVALS)
    result = execute_authorized_runtime_tool(
        "machine.move_path", arguments, source_context=_owner_context(machine["first"])
    )
    assert result is not None and not result.ok
    assert str(getattr(result, "status", "")) != "pending_approval", result.response_text
    assert set(_APPROVALS) == before  # no card was raised
    assert source.exists() and consent == []


# --- off Windows ---------------------------------------------------------------------------------------------


def test_off_windows_the_os_prompt_is_asked_exactly_once(machine, consent_yes, monkeypatch) -> None:
    from core.authorized_tool_execution import execute_authorized_runtime_tool

    _seams(monkeypatch, windows=False)
    source = machine["home"] / "Documents" / "notes.txt"
    source.write_bytes(b"hi\n")
    destination = machine["home"] / "Downloads" / "notes.txt"
    result = execute_authorized_runtime_tool(
        "machine.move_path", {"source": str(source), "destination": str(destination)},
        source_context=_owner_context(machine["home"] / "Documents"),
        permission_decision=_allow(exact=True, token_spent=True),
    )
    assert result is not None and result.ok, result.response_text
    assert not source.exists() and destination.read_bytes() == b"hi\n"
    assert len(consent_yes) == 1
