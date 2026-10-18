"""Windows changes the user's files only inside explicitly flagged disposable dev workspaces.

Owner decision (2026-10-18). Until the handle-based Windows filesystem authority exists, a path-checked
write can be raced, so Windows refuses user-filesystem writes unless every target lies inside a
folder listed in VOOL_WINDOWS_DISPOSABLE_WORKSPACES. The decision is driven here with
windows=True on every platform; the dispatcher test proves the real dispatch honours it.
"""
from __future__ import annotations

import os
from pathlib import Path

from core import windows_write_gate as gate


def _env(*roots: Path) -> dict[str, str]:
    return {gate.ENV_VAR: os.pathsep.join(str(root) for root in roots)}


def test_posix_is_never_gated(tmp_path: Path) -> None:
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, tmp_path, windows=False, environ={}) is None


def test_no_flagged_folder_refuses_every_user_filesystem_write(tmp_path: Path) -> None:
    for intent, args in (
        ("workspace.write_file", {"path": "a.txt", "content": "x"}),
        ("workspace.apply_unified_diff", {"patch": "..."}),
        ("workspace.rollback_last_change", {}),
        ("machine.write_file", {"path": str(tmp_path / "a.txt"), "content": "x"}),
        ("machine.move_path", {"source": str(tmp_path / "a"), "destination": str(tmp_path / "b")}),
        ("repo.git", {"operation": "commit"}),
    ):
        reason = gate.refusal(intent, args, tmp_path, windows=True, environ={})
        assert reason and "nothing was changed" in reason, intent


def test_reads_and_private_stores_are_not_gated(tmp_path: Path) -> None:
    for intent in ("workspace.read_file", "machine.read_file", "skill.create", "operator.save_note", "media.edit"):
        assert gate.refusal(intent, {"path": "a.txt"}, tmp_path, windows=True, environ={}) is None, intent


def test_a_flagged_workspace_allows_writes_inside_it(tmp_path: Path) -> None:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    env = _env(workspace)
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, workspace, windows=True, environ=env) is None
    assert gate.refusal("machine.write_file", {"path": str(workspace / "sub" / "b.txt")}, workspace,
                        windows=True, environ=env) is None


def test_every_target_must_be_inside_a_flagged_folder(tmp_path: Path) -> None:
    workspace, elsewhere = tmp_path / "scratch", tmp_path / "real-documents"
    workspace.mkdir()
    elsewhere.mkdir()
    env = _env(workspace)
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, elsewhere, windows=True, environ=env)
    assert gate.refusal("machine.move_path", {"source": str(workspace / "a"), "destination": str(elsewhere / "a")},
                        workspace, windows=True, environ=env)
    assert gate.refusal("machine.write_file", {"path": "..\\real-documents\\x.txt"}, workspace,
                        windows=True, environ=env)


def test_a_sibling_whose_name_extends_the_flagged_folder_is_not_inside_it(tmp_path: Path) -> None:
    workspace, sibling = tmp_path / "scratch", tmp_path / "scratch-real"
    workspace.mkdir()
    sibling.mkdir()
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, sibling, windows=True, environ=_env(workspace))


def test_a_relative_or_missing_flag_entry_flags_nothing(tmp_path: Path) -> None:
    env = {gate.ENV_VAR: os.pathsep.join(["scratch", str(tmp_path / "does-not-exist"), ""])}
    assert gate.flagged_roots(env) == []
    assert gate.refusal("workspace.write_file", {"path": "a.txt"}, tmp_path, windows=True, environ=env)


def test_the_real_dispatch_refuses_a_workspace_write_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    from core.runtime_execution_tools import _dispatch_runtime_tool

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _dispatch_runtime_tool("workspace.write_file", {"path": "a.txt", "content": "x"},
                                    source_context={"workspace_root": str(workspace)})

    assert result is not None and result.ok is False and result.status == gate.STATUS
    assert not (workspace / "a.txt").exists()


def test_the_real_dispatch_lets_a_flagged_workspace_through_the_gate(tmp_path: Path, monkeypatch) -> None:
    from core.runtime_execution_tools import _dispatch_runtime_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))

    result = _dispatch_runtime_tool("workspace.ensure_directory", {"path": "made"},
                                    source_context={"workspace_root": str(workspace)})

    assert result is not None and result.status != gate.STATUS


def test_an_operator_move_is_refused_outside_flagged_folders(tmp_path: Path, monkeypatch) -> None:
    from core.local_operator_actions import dispatch_operator_action
    from core.operator.models import OperatorActionIntent

    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.delenv(gate.ENV_VAR, raising=False)
    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    intent = OperatorActionIntent(kind="move_path", target_path=str(source), destination_path=str(tmp_path / "b.txt"))

    result = dispatch_operator_action(intent, task_id="t", session_id="s")

    assert result.ok is False and result.status == gate.STATUS
    assert source.exists() and not (tmp_path / "b.txt").exists()


def test_a_step_is_gated_by_its_inner_intent(tmp_path: Path) -> None:
    write_step = {"intent": "workspace.write_file", "arguments": {"path": "a.txt"}}
    read_step = {"intent": "workspace.read_file", "arguments": {"path": "a.txt"}}
    assert gate.refusal("code.task.step", write_step, tmp_path, windows=True, environ={})
    assert gate.refusal("code.task.step", read_step, tmp_path, windows=True, environ={}) is None
    assert gate.refusal("repo.step", write_step, tmp_path, windows=True, environ={})
