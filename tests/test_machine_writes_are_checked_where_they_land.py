"""A machine write is checked and shown where its writer puts it, never under the chat workspace.

machine.write_file and machine.ensure_directory take a relative path ("Documents\\x.txt"), an alias
("documents") and a `~` path from the user's home; machine.move_path and media.export take a
relative path from the process's working directory. Two decisions about these writes placed every
relative path under the chat workspace instead:

- The Windows write gate (core.windows_write_gate), which allows a user-filesystem write only inside
  the folders listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES. With the chat workspace flagged,
  machine.write_file "Documents\\x.txt" passed as a workspace file, and in Auto mode it was written
  to the user's real Documents folder.
- The Manual-mode approval ask, which showed "<workspace>/Documents/x.txt" while the write changed
  x.txt in the real Documents folder.

The Windows decision is driven on every platform (windows=True, or the gate's _IS_WINDOWS); the
Windows gauntlet runs this file on Windows too, with its own path rules.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from core import tool_intent_executor as executor
from core import windows_write_gate as gate
from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig

# A file in the user's Documents folder, named the ways a model names one, in this host's separator.
IN_DOCUMENTS = "Documents" + os.sep + "x.txt"
HOME_FORMS = (IN_DOCUMENTS, "~" + os.sep + IN_DOCUMENTS, "documents", "My Documents", "x.txt")


def _env(*roots: Path) -> dict[str, str]:
    return {gate.ENV_VAR: os.pathsep.join(str(root) for root in roots)}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A user home outside the chat workspace, moved where the machine writer looks for it."""
    from core import runtime_execution_tools

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # A redirect that did not take would let a write below land in the real Documents folder.
    assert runtime_execution_tools._machine_home() == home
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    return workspace


def _execute(intent: str, arguments: dict, source_context: dict):
    return executor.execute_tool_intent(
        {"intent": intent, "arguments": dict(arguments)},
        task_id=f"landing-{uuid.uuid4().hex}",
        session_id=f"landing-session-{uuid.uuid4().hex}",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=source_context,
        hive_activity_tracker=HiveActivityTracker(config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)),
    )


@pytest.mark.parametrize("path", HOME_FORMS)
@pytest.mark.parametrize("intent", ["machine.write_file", "machine.ensure_directory"])
def test_the_gate_checks_a_relative_machine_path_in_the_home_it_lands_in(home, workspace, intent, path) -> None:
    arguments = {"path": path, "content": "x"}
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace)), path
    # Flagging the home folder the path really lands in lets the same call through.
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace, home)) is None, path


@pytest.mark.skipif(os.name != "nt", reason="a drive-relative path such as C:x.txt exists only on Windows")
def test_the_gate_checks_a_drive_relative_machine_path_in_the_home_it_lands_in(home, workspace) -> None:
    # The writer joins "C:x.txt" onto the home on the same drive; joined onto the workspace it read as
    # a workspace file.
    arguments = {"path": home.drive + "x.txt", "content": "x"}
    assert gate.refusal("machine.write_file", arguments, workspace, windows=True, environ=_env(workspace))
    assert gate.refusal("machine.write_file", arguments, workspace, windows=True,
                        environ=_env(workspace, home)) is None


@pytest.mark.parametrize(
    ("intent", "arguments"),
    [
        ("machine.move_path", {"source": "a.txt", "destination": "b.txt"}),
        ("media.export", {"project_id": "clip", "output_path": "clip.mp4"}),
    ],
)
def test_the_gate_checks_a_relative_move_or_export_in_the_working_directory_it_lands_in(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch, intent: str, arguments: dict
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace))
    monkeypatch.chdir(workspace)
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace)) is None


def test_a_code_task_step_is_checked_where_its_machine_write_lands(home, workspace) -> None:
    step = {"intent": "machine.write_file", "arguments": {"path": IN_DOCUMENTS, "content": "x"}}
    assert gate.refusal("code.task.step", step, workspace, windows=True, environ=_env(workspace))
    assert gate.refusal("code.task.step", step, workspace, windows=True, environ=_env(workspace, home)) is None


def test_an_operator_path_must_still_be_absolute(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator lane names absolute paths. A relative one is refused even from inside a flagged folder.
    monkeypatch.chdir(workspace)
    arguments = {"source_path": "a.txt", "destination_path": "b.txt"}
    assert gate.refusal("operator.move_path", arguments, None, windows=True, environ=_env(workspace))


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_a_flagged_chat_workspace_no_longer_lets_a_write_into_the_real_documents_folder(
    home: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    decisions: list[dict] = []
    decide = executor.decide_tool_call
    monkeypatch.setattr(executor, "decide_tool_call", lambda *a, **k: decisions.append(k) or decide(*a, **k))

    execution = _execute("machine.write_file", {"path": IN_DOCUMENTS, "content": "x"},
                         {"workspace": str(workspace), "operating_mode": mode})

    assert execution.status == gate.STATUS, (execution.status, execution.response_text)
    assert decisions == [], "no approval is asked for a write Windows refuses"
    assert not (home / "Documents" / "x.txt").exists()


@pytest.mark.parametrize(
    ("intent", "path", "lands_at"),
    [
        ("machine.write_file", IN_DOCUMENTS, ("Documents", "x.txt")),
        ("machine.write_file", "<home>", ("Documents", "x.txt")),
        ("machine.ensure_directory", "Documents" + os.sep + "notes", ("Documents", "notes")),
    ],
)
def test_the_approval_ask_names_the_file_a_machine_write_changes(
    home: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch, intent: str, path: str, lands_at: tuple
) -> None:
    # Both folders are flagged, so on Windows the gate lets the call reach the approval ask.
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(workspace), str(home)]))
    named = str(home.joinpath(*lands_at)) if path == "<home>" else path

    execution = _execute(intent, {"path": named, "content": "x"}, {"workspace": str(workspace), "operating_mode": "manual"})

    assert execution.status == "pending_approval", (execution.status, execution.response_text)
    assert execution.details["approval_request"]["affected_resources"] == [str(home.joinpath(*lands_at).resolve())]
    assert not home.joinpath(*lands_at).exists()
