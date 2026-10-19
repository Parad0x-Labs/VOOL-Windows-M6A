"""A write is checked and shown where its writer puts it, never under the chat workspace.

machine.write_file and machine.ensure_directory take a relative path ("Documents\\x.txt"), an alias
("documents") and a `~` path from the user's home; machine.move_path and media.export take a
relative path from VOOL's working directory, and an export also writes stage files in the folder
that holds its output; the operator lane expands `$VAR` and `~` in its paths. Two decisions about
these writes placed a path somewhere other than its writer does, and a temp cleanup also empties
folders its request does not name:

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
#: The home folders a machine write may land in (runtime_execution_tools._SAFE_MACHINE_DIRECTORY_NAMES).
SAFE_FOLDERS = ("Desktop", "Documents", "Downloads")
#: A machine path as a model names it, and the folder of the home its writer puts it in ("" is the home).
HOME_FORMS = (
    (IN_DOCUMENTS, "Documents"),
    ("~" + os.sep + IN_DOCUMENTS, "Documents"),
    ("documents", "Documents"),
    ("My Documents", "Documents"),
    ("x.txt", ""),
)


def _env(*roots: Path) -> dict[str, str]:
    return {gate.ENV_VAR: os.pathsep.join(str(root) for root in roots)}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A user home outside the chat workspace, moved where the machine writer looks for it."""
    from core import runtime_execution_tools

    home = tmp_path / "home"
    for folder in SAFE_FOLDERS:
        (home / folder).mkdir(parents=True)
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


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """VOOL's working directory, outside the chat workspace."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


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


@pytest.mark.parametrize(("path", "folder"), HOME_FORMS)
@pytest.mark.parametrize("intent", ["machine.write_file", "machine.ensure_directory"])
def test_the_gate_checks_a_relative_machine_path_in_the_home_folder_it_lands_in(
    home: Path, workspace: Path, intent: str, path: str, folder: str
) -> None:
    arguments = {"path": path, "content": "x"}
    # Every other folder of the home is flagged, so a path placed in any wrong one is let through.
    neighbours = [home / name for name in SAFE_FOLDERS if name != folder]
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace)), path
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace, *neighbours)), path
    # Flagging the folder the path really lands in, and only that folder, lets the same call through.
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace, home / folder)) is None, path


def test_a_machine_path_that_climbs_is_checked_where_it_climbs_to(home: Path, workspace: Path) -> None:
    # "Documents/../x.txt" is x.txt in the home, not in Documents; "../x.txt" is beside the home.
    up_to_home = {"path": os.sep.join(["Documents", "..", "x.txt"]), "content": "x"}
    assert gate.refusal("machine.write_file", up_to_home, workspace, windows=True,
                        environ=_env(workspace, home / "Documents"))
    assert gate.refusal("machine.write_file", up_to_home, workspace, windows=True,
                        environ=_env(workspace, home)) is None
    out_of_home = {"path": os.sep.join(["..", "x.txt"]), "content": "x"}
    assert gate.refusal("machine.write_file", out_of_home, workspace, windows=True, environ=_env(workspace, home))


@pytest.mark.skipif(os.name != "nt", reason="a drive-relative path such as C:Documents\\x.txt exists only on Windows")
def test_the_gate_checks_a_drive_relative_machine_path_in_the_home_folder_it_lands_in(home: Path, workspace: Path) -> None:
    # The writer joins "C:Documents\\x.txt" onto the home, on the same drive; joined onto the
    # workspace it read as a workspace file.
    arguments = {"path": home.drive + IN_DOCUMENTS, "content": "x"}
    assert gate.refusal("machine.write_file", arguments, workspace, windows=True, environ=_env(workspace))
    assert gate.refusal("machine.write_file", arguments, workspace, windows=True,
                        environ=_env(workspace, home / "Downloads"))
    assert gate.refusal("machine.write_file", arguments, workspace, windows=True,
                        environ=_env(workspace, home / "Documents")) is None


@pytest.mark.parametrize(
    ("intent", "arguments"),
    [
        ("machine.move_path", {"source": "a.txt", "destination": "b.txt"}),
        ("media.export", {"project_id": "clip", "output_path": "clip.mp4"}),
    ],
)
def test_the_gate_checks_a_relative_move_or_export_in_the_working_directory_it_lands_in(
    workspace: Path, elsewhere: Path, intent: str, arguments: dict
) -> None:
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace))
    assert gate.refusal(intent, arguments, workspace, windows=True, environ=_env(workspace, elsewhere)) is None


def test_an_export_path_is_checked_unstripped_as_the_export_reads_it(home: Path, workspace: Path, elsewhere: Path) -> None:
    # The export does not strip its path, so " ~/clip.mp4" is in a folder named " ~" in the working
    # directory. Stripped first, the gate checked it in the home.
    arguments = {"project_id": "clip", "output_path": " ~" + os.sep + "clip.mp4"}
    assert gate.refusal("media.export", arguments, workspace, windows=True, environ=_env(workspace, home))
    assert gate.refusal("media.export", arguments, workspace, windows=True, environ=_env(workspace, elsewhere)) is None


def test_an_export_must_name_a_file_inside_a_flagged_folder_not_the_folder(workspace: Path) -> None:
    # An export writes its stage files in the folder that holds its output, so naming the flagged
    # folder itself would put them in the folder above it.
    the_folder = {"project_id": "clip", "output_path": str(workspace)}
    assert gate.refusal("media.export", the_folder, workspace, windows=True, environ=_env(workspace))
    a_file_in_it = {"project_id": "clip", "output_path": str(workspace / "clip.mp4")}
    assert gate.refusal("media.export", a_file_in_it, workspace, windows=True, environ=_env(workspace)) is None


def test_an_export_writes_only_where_the_gate_checked(
    tmp_path: Path, workspace: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real export, through the dispatcher and its gate. The media worker is a process of its own
    that needs ffmpeg, so a recorder of the files VOOL asks it to write stands in for it."""
    from core import media_studio_service as service
    from core.runtime_execution_tools import _dispatch_runtime_tool

    asked: list[str] = []

    def worker(op: str, params: dict, *, timeout: float | None = None) -> dict:
        if "dst" in params:
            asked.append(params["dst"])
        return {"duration": 6.0, "width": 320, "height": 240} if op == "probe" else {}

    monkeypatch.setattr(service, "_worker_call", worker)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    project = service.open_project(str(source))
    service.apply_edit(project.project_id, "trim", {"start": 1.0, "end": 2.0}, actor="chat")
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    arguments = {"project_id": project.project_id, "output_path": "clip.mp4"}

    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    refused = _dispatch_runtime_tool("media.export", dict(arguments), source_context={"workspace_root": str(workspace)})
    assert refused is not None and refused.status == gate.STATUS
    assert asked == []

    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(workspace), str(elsewhere)]))
    exported = _dispatch_runtime_tool("media.export", dict(arguments), source_context={"workspace_root": str(workspace)})
    assert exported is not None and exported.ok, exported and exported.response_text
    # A stage file and then the output, each named absolute, so the worker writes them in the folder the
    # gate checked whatever its own working directory is.
    assert len(asked) == 2, asked
    assert all(Path(dst).is_absolute() and Path(dst).parent.resolve() == elsewhere.resolve() for dst in asked), asked
    assert Path(asked[-1]).resolve() == (elsewhere / "clip.mp4").resolve()
    assert exported.details["export_receipt"]["output_path"] == asked[-1]


def test_a_code_task_step_is_checked_where_its_machine_write_lands(home: Path, workspace: Path) -> None:
    step = {"intent": "machine.write_file", "arguments": {"path": IN_DOCUMENTS, "content": "x"}}
    assert gate.refusal("code.task.step", step, workspace, windows=True, environ=_env(workspace))
    assert gate.refusal("code.task.step", step, workspace, windows=True, environ=_env(workspace, home / "Downloads"))
    assert gate.refusal("code.task.step", step, workspace, windows=True,
                        environ=_env(workspace, home / "Documents")) is None


def test_an_operator_path_is_checked_where_its_variables_and_home_expand_to(
    home: Path, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator lane expands `$VAR` and `~` in a path before it moves anything (core.operator.storage).
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("VOOL_TEST_CLIMB", os.sep.join(["..", "outside"]))
    climbs = {"source_path": str(workspace / "a.txt"), "destination_path": str(workspace / "$VOOL_TEST_CLIMB")}
    assert gate.refusal("operator.move_path", climbs, None, windows=True, environ=_env(workspace))
    assert gate.refusal("operator.move_path", climbs, None, windows=True, environ=_env(workspace, outside)) is None
    in_home = {"source_path": "~" + os.sep + "a.txt", "destination_path": "~" + os.sep + "Documents"}
    assert gate.refusal("operator.move_path", in_home, None, windows=True, environ=_env(workspace))
    assert gate.refusal("operator.move_path", in_home, None, windows=True, environ=_env(home)) is None


def test_an_operator_path_must_still_be_absolute(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator lane would take a relative path from VOOL's working directory. The gate does not
    # place it there, so it refuses one even from inside a flagged folder.
    monkeypatch.chdir(workspace)
    arguments = {"source_path": "a.txt", "destination_path": "b.txt"}
    reason = gate.refusal("operator.move_path", arguments, None, windows=True, environ=_env(workspace))
    assert reason and "needs a source_path that is absolute" in reason


def test_windows_refuses_a_temp_cleanup_even_from_a_flagged_folder(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup also empties the system temp folders, and its approval empties the plan a disk scan
    stored earlier (core.operator.handlers), so naming a flagged folder does not hold it there."""
    import tempfile

    from core import local_operator_actions as ops

    temp = tmp_path / "temp"  # the system temp folder, outside the flagged one
    temp.mkdir()
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(temp))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    # A regression must not be able to empty a real folder, so emptying is recorded, not done.
    emptied: list[str] = []
    monkeypatch.setattr(ops, "_delete_children",
                        lambda root: emptied.append(str(root)) or {"deleted_files": 0, "deleted_dirs": 0, "errors": []})
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    session = f"cleanup-{uuid.uuid4().hex}"

    def say(text: str):
        return ops.dispatch_operator_action(ops.parse_operator_action_intent(text), task_id=f"t-{uuid.uuid4().hex}",
                                            session_id=session)

    say(f'find disk bloat in "{workspace}"')  # stores a cleanup plan of the temp folder
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    refused = say(f'clean all temp files in "{workspace}"')
    assert refused.status == gate.STATUS, (refused.status, refused.response_text)
    # Refused as a cleanup, not for a path the gate could not read: the path named is absolute.
    assert "does not clean temp files" in refused.response_text, refused.response_text
    assert emptied == []
    # Off Windows the same words empty the stored plan, so the refusal above is the gate's.
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    say(f'clean all temp files in "{workspace}"')
    assert emptied == [str(temp)]


@pytest.mark.parametrize(
    ("intent", "arguments"),
    [
        ("machine.write_file", {"path": "{workspace}/a\x00.txt"}),
        ("machine.move_path", {"source": "{workspace}/a\x00.txt", "destination": "{workspace}/b.txt"}),
        ("media.export", {"project_id": "clip", "output_path": "{workspace}/a\x00.mp4"}),
        ("operator.move_path", {"source_path": "{workspace}/a\x00.txt", "destination_path": "{workspace}"}),
    ],
)
def test_a_path_no_file_can_have_is_refused_not_raised(workspace: Path, intent: str, arguments: dict) -> None:
    # A NUL inside a flagged folder: placing it raised in the gate instead of refusing the call.
    named = {key: value.replace("{workspace}", str(workspace)) for key, value in arguments.items()}
    assert gate.refusal(intent, named, workspace, windows=True, environ=_env(workspace))


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


def test_the_approval_ask_for_a_code_task_step_names_the_file_its_machine_write_changes(
    home: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(gate.ENV_VAR, os.pathsep.join([str(workspace), str(home)]))
    step = {"task_id": "ct-landing", "intent": "machine.write_file", "arguments": {"path": IN_DOCUMENTS, "content": "x"}}

    execution = _execute("code.task.step", step, {"workspace": str(workspace), "operating_mode": "manual"})

    assert execution.status == "pending_approval", (execution.status, execution.response_text)
    assert execution.details["approval_request"]["affected_resources"] == [str((home / IN_DOCUMENTS).resolve())]
    assert not (home / IN_DOCUMENTS).exists()
