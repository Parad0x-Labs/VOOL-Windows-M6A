"""On Windows, approving a pending move is judged on the paths that move will act on.

The move handler answers a session's pending move from the preview's stored source and folder,
never from the approval turn's text. The Windows write gate was asked about the turn's own paths, so
"approve move <id>" (which names none) was refused "names no source_path" and no move could ever be
approved on Windows, and a later move request naming a relative file was refused before the handler
could say the pending move still needs approval. These tests drive the gate's Windows decision on any
host and stand in for the pending-action store.
"""
from __future__ import annotations

import json

import pytest

from core import local_operator_actions as ops
from core import windows_write_gate as gate
from core.local_operator_actions import OperatorActionIntent

APPROVE = OperatorActionIntent(kind="move_path", approval_requested=True, action_id="mv-1", raw_text="approve move mv-1")


@pytest.fixture
def windows(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "desk").mkdir(parents=True)
    (workspace / "archive").mkdir()
    monkeypatch.setattr(gate, "_IS_WINDOWS", True)
    monkeypatch.setenv(gate.ENV_VAR, str(workspace))
    return workspace


def _pending(monkeypatch, scope, seen=None):
    def _load(*, session_id, action_kind, action_id=None):
        if seen is not None:
            seen.append((session_id, action_kind, action_id))
        return None if scope is None else {"action_id": "mv-1", "scope_json": json.dumps(scope)}

    monkeypatch.setattr(ops, "_load_pending_action", _load)


def _scope(source, folder):
    return {"source_path": str(source), "destination_dir": str(folder), "destination_path": str(folder / source.name)}


def test_an_approval_of_a_pending_move_inside_a_flagged_folder_is_not_refused(windows, monkeypatch):
    seen = []
    _pending(monkeypatch, _scope(windows / "desk" / "report.txt", windows / "archive"), seen)
    assert ops.windows_write_refusal(APPROVE, session_id="sess-1") is None
    assert seen == [("sess-1", "move_path", "mv-1")]


def test_without_a_session_an_approval_is_still_refused_for_naming_no_path(windows, monkeypatch):
    _pending(monkeypatch, _scope(windows / "desk" / "report.txt", windows / "archive"))
    reason = ops.windows_write_refusal(APPROVE)
    assert reason and "names no source_path" in reason


def test_a_pending_move_leaving_the_flagged_folders_is_refused_at_approval(windows, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    _pending(monkeypatch, _scope(windows / "desk" / "report.txt", outside))
    assert ops.windows_write_refusal(APPROVE, session_id="sess-1")


def test_a_pending_move_whose_final_file_lands_outside_is_refused(windows, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = windows / "desk" / "report.txt"
    scope = {"source_path": str(source), "destination_dir": str(windows / "archive"),
             "destination_path": str(outside / "report.txt")}
    _pending(monkeypatch, scope)
    assert ops.windows_write_refusal(APPROVE, session_id="sess-1")


def test_a_pending_move_with_no_final_file_is_refused(windows, monkeypatch):
    source = windows / "desk" / "report.txt"
    _pending(monkeypatch, {"source_path": str(source), "destination_dir": str(windows / "archive")})
    reason = ops.windows_write_refusal(APPROVE, session_id="sess-1")
    assert reason and "names no destination_path" in reason


@pytest.mark.parametrize("scope", [{}, {"source_path": "", "destination_dir": "x"}, ["not", "a", "scope"]])
def test_a_stored_preview_missing_its_paths_is_refused(windows, monkeypatch, scope):
    monkeypatch.setattr(ops, "_load_pending_action",
                        lambda **_kw: {"action_id": "mv-1", "scope_json": json.dumps(scope)})
    assert ops.windows_write_refusal(APPROVE, session_id="sess-1")


def test_an_unreadable_store_falls_back_to_the_turns_own_paths(windows, monkeypatch):
    def _broken(**_kw):
        raise OSError("database is locked")

    monkeypatch.setattr(ops, "_load_pending_action", _broken)
    reason = ops.windows_write_refusal(APPROVE, session_id="sess-1")
    assert reason and "names no source_path" in reason


def test_a_later_request_naming_a_relative_file_is_judged_on_the_pending_move(windows, monkeypatch):
    _pending(monkeypatch, _scope(windows / "desk" / "report.txt", windows / "archive"))
    later = OperatorActionIntent(kind="move_path", target_path="Proceeds 2026.xlsx", destination_path="Finance",
                                 raw_text='move "Proceeds 2026.xlsx" to "Finance"')
    assert ops.windows_write_refusal(later, session_id="sess-1") is None


def test_with_no_pending_move_the_turns_own_paths_are_gated(windows, monkeypatch):
    _pending(monkeypatch, None)
    later = OperatorActionIntent(kind="move_path", target_path="Proceeds 2026.xlsx", destination_path="Finance")
    reason = ops.windows_write_refusal(later, session_id="sess-1")
    assert reason and "needs a source_path that is absolute" in reason


def test_off_windows_nothing_is_looked_up(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_IS_WINDOWS", False)
    seen = []
    _pending(monkeypatch, {}, seen)
    assert ops.windows_write_refusal(APPROVE, session_id="sess-1") is None
    assert seen == []


def test_the_dispatcher_passes_its_session(monkeypatch):
    seen = []

    def _spy(intent, *, session_id=None):
        seen.append(session_id)
        return "refused for the test"

    monkeypatch.setattr(ops, "windows_write_refusal", _spy)
    result = ops.dispatch_operator_action(APPROVE, task_id="t-1", session_id="sess-dispatch")
    assert seen == ["sess-dispatch"] and result.ok is False
