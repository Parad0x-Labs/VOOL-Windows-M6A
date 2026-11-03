"""Runtime writers run their A8 availability checks on one connection per write.

Gauntlet harness_control profile (Linux, one child, 394 s): append_runtime_event and
update_runtime_checkpoint checked every string in their trees on a NEW SQLite connection, so
111,652 connections cost 174 s of the run, mostly opening WAL. The serve path already shares one
connection per projection (core.runtime_continuity._serve_event_rows). Verdicts stay per-check and
uncached, so a governed string is still suppressed or refused.
"""

from __future__ import annotations

import pytest

import storage.db as sdb


@pytest.fixture()
def runtime_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("VOOL_HOME", str(home))
    from core.runtime_paths import configure_runtime_home

    configure_runtime_home(home)
    sdb.configure_default_db_path(tmp_path / "a8.db")
    from core import task_state_machine, trace_id

    monkeypatch.setattr(trace_id, "_TABLE_READY", False)
    monkeypatch.setattr(task_state_machine, "_TABLE_READY", False)
    from storage.migrations import run_migrations

    run_migrations()
    from core.runtime_continuity import configure_runtime_continuity_db_path
    from storage.db import active_default_db_path

    configure_runtime_continuity_db_path(active_default_db_path())
    yield home
    sdb.configure_default_db_path(None)
    configure_runtime_home(None)


def _count_availability_connections(monkeypatch) -> list[int]:
    import core.finalization as fin

    opened: list[int] = []
    real = fin.get_connection

    def counting(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(fin, "get_connection", counting)
    return opened


def _tree(count: int) -> dict:
    return {"notes": [f"plain note number {index}" for index in range(count)], "label": "tree"}


def test_one_runtime_event_checks_its_whole_tree_on_one_connection(runtime_env, monkeypatch):
    from core.runtime_continuity import append_runtime_event

    opened = _count_availability_connections(monkeypatch)
    stored = append_runtime_event(
        session_id="s-one-conn", event_type="status", message="hello", details=_tree(40)
    )
    assert stored["notes"][39] == "plain note number 39"
    assert len(opened) <= 1, f"{len(opened)} availability connections for one event"


def test_one_checkpoint_update_checks_its_whole_tree_on_one_connection(runtime_env, monkeypatch):
    from core.runtime_continuity import create_runtime_checkpoint, update_runtime_checkpoint

    checkpoint = create_runtime_checkpoint(session_id="s-one-conn", request_text="one connection")
    opened = _count_availability_connections(monkeypatch)
    updated = update_runtime_checkpoint(
        checkpoint["checkpoint_id"],
        source_context=_tree(40),
        final_response="a plain answer",
        status="completed",
    )
    assert updated is not None and updated["final_response"] == "a plain answer"
    assert len(opened) <= 1, f"{len(opened)} availability connections for one checkpoint update"
