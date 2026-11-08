"""Windows: a write the owner asks for inside a folder they marked reaches the approval prompt and lands.

Captest B3 (installed 0.7.1-beta, 2026-11-08). The folder was marked in Settings → Folders VOOL may
change and VOOL restarted; then "create hello.txt in <folder> with the text hi" was asked four times in
three marked places. No write landed and no approval prompt appeared:

1. A path with spaces (`C:\\Users\\VOOLT\\VOOL Test\\Allowed Ä folder`) was cut at the first space.
2. A marked folder outside Desktop, Documents, Downloads and the workspace was dropped from the roots
   chat may write, and the reply said "no folder I can write to is marked yet".
3. Inside Documents the builder read `C:\\Users\\...` as a folder named `C` in the chat's workspace; the
   gate refused that write, and the reply still said "that is all I wrote".

The Windows decisions are driven on Linux through the gate's `_IS_WINDOWS` flag, as the gate's other
tests do; the marked folder is listed through VOOL_WINDOWS_DISPOSABLE_WORKSPACES, which the owner's
marks feed at start (core/windows_disposable_store.py). Drive-letter spellings are checked as text only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from core import windows_write_gate as gate
from core.agent_runtime import write_root_honesty


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A home with its own Desktop, Documents and Downloads; the chat's workspace (unmarked, as an
    unbound chat's Desktop is); and a marked folder with spaces and a non-ASCII letter in its path,
    outside all of them."""
    home = tmp_path / "home"
    for name in ("Desktop", "Documents", "Downloads"):
        (home / name).mkdir(parents=True)
    workspace = tmp_path / "OneDrive" / "Desktop"
    workspace.mkdir(parents=True)
    marked = tmp_path / "profile" / "VOOL Test" / "Allowed Ä folder"
    marked.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(marked))
    return {"home": home, "workspace": workspace, "marked": marked}


def _ask(folder: Path, *, quoted: bool = False) -> str:
    where = f'"{folder}"' if quoted else str(folder)
    return f"create hello.txt in {where} with the text hi"


# --- 1. a path with spaces and a non-ASCII letter is read whole --------------------------------


@pytest.mark.parametrize("quoted", [False, True])
def test_a_marked_folder_with_spaces_is_read_whole(machine, quoted: bool) -> None:
    marked = machine["marked"]
    assert write_root_honesty.write_destinations(_ask(marked, quoted=quoted)) == [str(marked)]
    named_file = f"create {marked}{os.sep}hello.txt with the text hi"
    assert write_root_honesty.write_destinations(named_file) == [f"{marked}{os.sep}hello.txt"]


def test_words_after_the_folder_are_not_taken_into_its_name(machine) -> None:
    marked = machine["marked"]
    # "with" would only make up a new name, so the path ends at the folder that exists.
    assert write_root_honesty.write_destinations(f"save {marked} with the text hi") == [str(marked)]
    # Nothing on disk to extend through: the path ends at the space, as before.
    assert write_root_honesty.write_destinations("create /nowhere/VOOL Test/x.txt") == ["/nowhere/VOOL"]


# --- 2. a marked folder anywhere is a root chat may write --------------------------------------


def test_a_marked_folder_outside_the_known_folders_is_writable(machine) -> None:
    context = {"workspace": str(machine["workspace"])}
    roots = [os.path.normcase(str(root)) for root in write_root_honesty.allowed_write_roots(context)]
    assert roots == [os.path.normcase(os.path.realpath(machine["marked"]))]
    assert write_root_honesty.unreachable_write_target(_ask(machine["marked"]), source_context=context) == ""
    reply = write_root_honesty.unreachable_write_response("/elsewhere/x.txt", source_context=context)
    assert "no folder I can write to is marked" not in reply
    assert os.path.realpath(machine["marked"]) in reply


def test_the_gate_allows_a_machine_write_in_a_marked_folder_while_the_workspace_is_unmarked(machine) -> None:
    target = str(machine["marked"] / "hello.txt")
    assert gate.refusal("machine.write_file", {"path": target, "content": "hi"}, machine["workspace"]) is None
    assert gate.refusal("machine.ensure_directory", {"path": str(machine["marked"] / "sub")}, machine["workspace"]) is None


def test_the_gate_still_refuses_everything_outside_the_marked_folders(machine) -> None:
    workspace, marked = machine["workspace"], machine["marked"]
    sibling = marked.parent / (marked.name + "-x")
    sibling.mkdir()
    for path in (workspace / "hello.txt", sibling / "hello.txt", machine["home"] / "Documents" / "hello.txt",
                 marked / ".." / "hello.txt"):
        assert gate.refusal("machine.write_file", {"path": str(path), "content": "hi"}, workspace), path
    # A workspace write still needs the workspace itself marked.
    assert gate.refusal("workspace.write_file", {"path": "hello.txt", "content": "hi"}, workspace)


class _Agent:
    """The two calls the machine fast path makes on the agent."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _emit_runtime_event(self, _context, **event) -> None:
        self.events.append(event)

    def _fast_path_result(self, **kwargs) -> dict[str, Any]:
        return {"response": kwargs.get("response"), "reason": kwargs.get("reason")}


def _lane(text: str, workspace: Path, mode: str) -> dict[str, Any] | None:
    from core.agent_runtime.fast_paths_machine import maybe_handle_direct_machine_write_request

    return maybe_handle_direct_machine_write_request(
        _Agent(), text, session_id="marked-folder", source_surface="api",
        source_context={"workspace": str(workspace), "operating_mode": mode},
    )


@pytest.mark.parametrize("quoted", [False, True])
def test_a_write_to_a_marked_folder_reaches_the_approval_prompt(machine, quoted: bool) -> None:
    result = _lane(_ask(machine["marked"], quoted=quoted), machine["workspace"], "manual")
    assert result is not None
    assert result.get("approval_request"), result
    assert result.get("task_outcome") == "pending_approval"
    assert not (machine["marked"] / "hello.txt").exists()
    assert not (machine["workspace"] / "hello.txt").exists()


def test_an_allowed_write_to_a_marked_folder_lands_and_is_read_back(machine) -> None:
    result = _lane(_ask(machine["marked"]), machine["workspace"], "auto")
    assert result is not None, "no lane took the write"
    landed = machine["marked"] / "hello.txt"
    assert landed.read_bytes() == b"hi"
    assert "read it back" in str(result["response"]) and str(landed) in str(result["response"])
    assert not (machine["workspace"] / "hello.txt").exists()
    assert not (machine["workspace"] / "C").exists()


def test_a_write_outside_the_marked_folders_is_not_taken_by_the_marked_lane(machine, tmp_path: Path) -> None:
    from core.agent_runtime.fast_paths_machine import _extract_marked_folder_write_target

    other = tmp_path / "profile" / "Not marked"
    other.mkdir()
    assert _extract_marked_folder_write_target(_ask(other)) is None
    assert write_root_honesty.unreachable_write_target(
        _ask(other), source_context={"workspace": str(machine["workspace"])}
    ) == str(other)
    assert not (other / "hello.txt").exists()


def test_off_windows_the_marked_lane_takes_nothing(machine, monkeypatch) -> None:
    from core.agent_runtime.fast_paths_machine import _extract_marked_folder_write_target

    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    assert _extract_marked_folder_write_target(_ask(machine["marked"])) is None


def test_a_brief_for_a_marked_folder_outside_the_workspace_is_answered_not_relocated(machine) -> None:
    text = f"create notes.md in {machine['marked']} containing a summary of the project"
    result = _lane(text, machine["workspace"], "auto")
    assert result is not None
    assert "Nothing was written" in str(result["response"])
    assert list(machine["marked"].iterdir()) == [] and list(machine["workspace"].iterdir()) == []


# --- 3. a drive-absolute path is never re-rooted, and the reply claims only what landed --------


@pytest.mark.parametrize(
    "text",
    [
        r"create hello.txt in C:\Users\VOOLT\Documents\Test with the text hi",
        r"create hello.txt in C:\Users\VOOLT\VOOL Test\Allowed Ä folder with the text hi",
        "create hello.txt in C:/Users/VOOLT/Documents with the text hi",
    ],
)
def test_a_drive_letter_is_never_a_folder_in_the_workspace(tmp_path: Path, text: str) -> None:
    from core.agent_runtime.fast_paths_builder import extract_requested_builder_root

    assert extract_requested_builder_root(text, workspace_root=str(tmp_path)) == ""


def test_a_refused_exact_write_is_not_reported_as_written() -> None:
    from core.agent_runtime.builder import app_builder
    from core.agent_runtime.builder import mutation_scope

    scope = mutation_scope.MutationScope(kind=mutation_scope.EXACT, paths=("hello.txt",), allow_commands=False)
    refusal = "This target is outside the marked folders; nothing was changed."

    def run_tool(intent: str, arguments: dict, **_kwargs):
        return app_builder.ToolOutcome(ok=False, response_text=refusal)

    report = app_builder.build_app_from_spec(
        request="create hello.txt with the text hi", target_rel="", source_context={},
        generate_fn=lambda _prompt: "hi", run_tool_fn=run_tool, scope=scope,
    )
    assert report.files_written == [] and report.files_skipped == ["hello.txt"]
    reply = app_builder.render_app_build_response(report)
    assert "that is all I wrote" not in reply
    assert "Nothing was written" in reply and "outside the marked folders" in reply


def test_a_written_exact_file_is_still_reported() -> None:
    from core.agent_runtime.builder import app_builder
    from core.agent_runtime.builder import mutation_scope

    scope = mutation_scope.MutationScope(kind=mutation_scope.EXACT, paths=("hello.txt",), allow_commands=False)
    report = app_builder.build_app_from_spec(
        request="create hello.txt with the text hi", target_rel="", source_context={},
        generate_fn=lambda _prompt: "hi", run_tool_fn=lambda *a, **k: app_builder.ToolOutcome(ok=True), scope=scope,
    )
    assert report.files_written == ["hello.txt"]
    assert "so that is all I wrote" in app_builder.render_app_build_response(report)


# --- the served chat, end to end -----------------------------------------------------------------


def test_the_served_chat_asks_to_approve_the_write_in_the_marked_folder(machine, tmp_path: Path, monkeypatch) -> None:
    """The turn as the window sends it: /api/chat, an unbound chat whose workspace is the (unmarked)
    Desktop, the folder named without quotes. It reaches the approval prompt for that exact file."""
    import json
    import uuid

    from apps import vool_api_server
    from apps.vool_agent import VoolAgent
    from core import runtime_paths
    from core.web.api.runtime import RuntimeServices
    from tests.asgi_harness import asgi_request

    monkeypatch.setenv("VOOL_HOME", str(tmp_path / "vool-home"))
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(machine["workspace"]))
    runtime_paths.configure_runtime_home(tmp_path / "vool-home")
    try:
        agent = VoolAgent(backend_name="test-backend", device="marked-folder", persona_id="default")
        monkeypatch.setattr(vool_api_server, "_agent", agent)
        app = vool_api_server.create_app(RuntimeServices(display_name="VOOL", agent=agent))
        body = {
            "messages": [{"role": "user", "content": _ask(machine["marked"])}],
            "session_id": f"openclaw:{uuid.uuid4().hex[:20]}",
            "stream": False,
            "workspace": str(machine["workspace"]),
        }
        status, _headers, raw = asgi_request(
            app, method="POST", path="/api/chat",
            headers={"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1:11435"},
            body=json.dumps(body).encode("utf-8"),
        )
    finally:
        runtime_paths.configure_runtime_home(None)
    reply = str((json.loads(raw.decode("utf-8")).get("message") or {}).get("content") or "")
    assert status == 200
    assert "needs your approval" in reply and str(machine["marked"] / "hello.txt") in reply, reply
    assert "no folder I can write to is marked" not in reply
    assert not (machine["marked"] / "hello.txt").exists()
    assert list(machine["workspace"].iterdir()) == []
