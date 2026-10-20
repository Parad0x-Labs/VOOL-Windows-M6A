"""Archaeology reads stay inside the workspace.

``archaeology.compare`` (kind ``files``) joined its operands onto the workspace root
unchecked, so an absolute operand, a ``..`` operand or a link inside the workspace read
any file on the machine. Every archaeology command also took ``workspace_root`` from
the model as given, so the model could walk and excerpt any folder it named.

- compare operands resolve inside ``workspace_root`` for every caller;
- the model door confines ``workspace_root`` to the folder its chat is bound to (the
  trusted ``source_context``, else the configured workspace), like the workspace tools;
- an outside root is refused before the filesystem sees it (on Windows a UNC path would
  reach the network).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

OUTSIDE_MARKER = "ARCH-OUTSIDE-READ-MARKER-5e21"


@pytest.fixture()
def outside_file(arch_home) -> Path:
    target = arch_home["tmp"] / "elsewhere" / "outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{OUTSIDE_MARKER}\n", encoding="utf-8")
    return target


def _model_call(command: str, arguments: dict, *, workspace: Path | None):
    """One model tool call through the executor a served chat turn uses."""
    from core import tool_intent_executor as executor
    from core.command_registry.model_tools import project_model_tools
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig

    project_model_tools()  # the contracts a served turn offers the model
    context = {"operating_mode": "auto"}  # read-only calls run without a prompt
    if workspace is not None:
        context["workspace"] = str(workspace)
    return executor.execute_tool_intent(
        {"intent": f"operator.command.archaeology.{command}", "arguments": dict(arguments)},
        task_id=f"arch-scope-{uuid.uuid4().hex}",
        session_id="sess-scope",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context=context,
        hive_activity_tracker=HiveActivityTracker(
            config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)
        ),
    )


def _assert_refused_without_leak(execution) -> None:
    envelope = execution.details["envelope"]
    assert execution.ok is False, envelope
    fault = envelope["fault"] or {}
    assert fault.get("code") == "fault_validation", fault
    assert (fault.get("detail") or {}).get("scope_refused") is True, fault
    assert OUTSIDE_MARKER not in repr(envelope), envelope


# -- compare operands (every caller) ------------------------------------------


def test_compare_files_refuses_an_absolute_operand_outside_the_workspace(workspace, outside_file):
    import core.project_archaeology as pa

    with pytest.raises(pa.ScopeRefused):
        pa.compare(
            kind="files", a=str(outside_file), b="src/config.py", workspace_root=str(workspace)
        )


def test_compare_files_refuses_a_dotdot_operand(workspace, outside_file):
    import core.project_archaeology as pa

    escape = os.path.relpath(outside_file, workspace)
    assert escape.startswith(".."), escape
    with pytest.raises(pa.ScopeRefused):
        pa.compare(kind="files", a=escape, b="src/config.py", workspace_root=str(workspace))


def test_compare_files_refuses_a_link_that_points_outside(workspace, outside_file):
    import core.project_archaeology as pa

    os.symlink(outside_file, workspace / "link-outside")
    with pytest.raises(pa.ScopeRefused):
        pa.compare(kind="files", a="link-outside", b="src/config.py", workspace_root=str(workspace))


# -- the model door: workspace_root stays inside the bound folder ---------------


def test_model_compare_refuses_a_workspace_root_outside_the_bound_folder(workspace, outside_file):
    execution = _model_call(
        "compare",
        {"kind": "files", "a": "outside.txt", "b": "outside.txt",
         "workspace_root": str(outside_file.parent)},
        workspace=workspace,
    )
    _assert_refused_without_leak(execution)


def test_model_compare_refuses_an_absolute_operand_outside_the_bound_folder(workspace, outside_file):
    execution = _model_call(
        "compare",
        {"kind": "files", "a": str(outside_file), "b": "src/config.py",
         "workspace_root": str(workspace)},
        workspace=workspace,
    )
    _assert_refused_without_leak(execution)


@pytest.mark.parametrize("command", ["search", "locate", "trace", "recovery_plan"])
def test_model_walks_refuse_a_workspace_root_outside_the_bound_folder(
    command, workspace, outside_file
):
    arguments = {
        "search": {"text": OUTSIDE_MARKER},
        "locate": {"reference": "outside.txt"},
        "trace": {"path": "outside.txt"},
        "recovery_plan": {"path": "outside.txt"},
    }[command]
    arguments["workspace_root"] = str(outside_file.parent)
    _assert_refused_without_leak(_model_call(command, arguments, workspace=workspace))


def test_model_search_refuses_a_dotdot_workspace_root(workspace, outside_file):
    escape = str(workspace / os.path.relpath(outside_file.parent, workspace))
    execution = _model_call(
        "search", {"text": OUTSIDE_MARKER, "workspace_root": escape}, workspace=workspace
    )
    _assert_refused_without_leak(execution)


def test_model_without_a_bound_folder_is_held_to_the_configured_workspace(
    workspace, outside_file, monkeypatch
):
    monkeypatch.setenv("VOOL_WORKSPACE_ROOT", str(workspace))
    execution = _model_call(
        "search",
        {"text": OUTSIDE_MARKER, "workspace_root": str(outside_file.parent)},
        workspace=None,
    )
    _assert_refused_without_leak(execution)


def test_model_outside_root_is_refused_before_the_filesystem_sees_it(
    workspace, outside_file, monkeypatch
):
    # The lexical check must come first: on Windows a UNC root would reach the network
    # on the first exists()/realpath() call.
    outside = str(outside_file.parent)
    real_realpath = os.path.realpath
    real_exists = Path.exists
    touched: list[str] = []

    def spy_realpath(path, *args, **kwargs):
        if str(path).startswith(outside):
            touched.append(str(path))
        return real_realpath(path, *args, **kwargs)

    def spy_exists(self, *args, **kwargs):
        if str(self).startswith(outside):
            touched.append(str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", spy_realpath)
    monkeypatch.setattr(Path, "exists", spy_exists)
    execution = _model_call(
        "search", {"text": OUTSIDE_MARKER, "workspace_root": outside}, workspace=workspace
    )
    monkeypatch.undo()
    _assert_refused_without_leak(execution)
    assert touched == [], touched


# -- guards: reads inside the bound folder keep working ---------------------------


def test_model_compare_inside_the_bound_folder_still_reads(workspace):
    execution = _model_call(
        "compare",
        {"kind": "files", "a": "reports/night-report.md",
         "b": str(workspace / "src" / "config.py"), "workspace_root": str(workspace)},
        workspace=workspace,
    )
    assert execution.ok is True, execution.details["envelope"]
    comparison = execution.details["envelope"]["data"]["comparison"]
    assert "VERBATIM-NIGHT-REPORT-MARKER-7f3a" in comparison["a"]["excerpt"]


def test_model_search_in_a_subfolder_of_the_bound_folder_still_walks(workspace):
    execution = _model_call(
        "search",
        {"text": "VERBATIM-NIGHT-REPORT-MARKER-7f3a", "workspace_root": str(workspace / "reports")},
        workspace=workspace,
    )
    assert execution.ok is True, execution.details["envelope"]
    paths = [row.get("path") for row in execution.details["envelope"]["data"]["results"]]
    assert "night-report.md" in paths, paths


# -- links the walk must not follow ------------------------------------------------


class _JunctionLike:
    """A directory entry as Windows reports a junction: is_symlink() False, is_junction() True,
    is_dir() following it."""

    def __init__(self, entry) -> None:
        self._entry = entry

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def is_symlink(self) -> bool:
        return False

    def is_junction(self) -> bool:
        return True


def test_the_walk_never_follows_a_junction_out_of_the_workspace(workspace, outside_file, monkeypatch):
    import core.project_archaeology as pa

    os.symlink(outside_file.parent, workspace / "junction-out", target_is_directory=True)
    real_scandir = os.scandir

    def scandir_with_junction(path):
        return [
            _JunctionLike(entry) if entry.name == "junction-out" else entry
            for entry in real_scandir(path)
        ]

    monkeypatch.setattr(pa.os, "scandir", scandir_with_junction)
    envelope = pa.search(text=OUTSIDE_MARKER, workspace_root=str(workspace))
    monkeypatch.undo()

    assert OUTSIDE_MARKER not in repr(envelope["results"]), envelope["results"]
    assert any(row.get("path") == "junction-out" for row in envelope["scope"]["refused"]), envelope["scope"]
