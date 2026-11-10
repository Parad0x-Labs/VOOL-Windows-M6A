"""A journal entry whose recorded root is not on this machine never answers a path query.

Such an entry cannot be canonicalized, so VOOL cannot tell whether it names the queried file or a file
with the same relative path in another workspace. It fails closed: the plan says NOT RECOVERABLE and
names the root the entry was recorded under. A relative path with no workspace root to read it in
matches nothing either.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import core.project_archaeology as pa
from core.command_registry.execute import ExecutionContext, execute_command

GONE_ROOT = "/no/such/vool-recorded-root"


@pytest.fixture()
def ws(arch_home, blackbox) -> Path:
    root = (arch_home["tmp"] / "real-ws").resolve()
    (root / "lost").mkdir(parents=True)
    (root / "kept").mkdir(parents=True)
    (root / "kept" / "k.txt").write_text("k\n", encoding="utf-8")
    for effect_id, entry_root, path in (
        ("eff-gone-01", GONE_ROOT, "lost/gone.txt"),
        ("eff-kept-02", str(root), "kept/k.txt"),
    ):
        for kind in ("effect_intended", "effect_terminal"):
            entry = {
                "schema": "blackbox_effect_v1", "kind": kind,
                "effect_id": effect_id, "turn_id": f"turn-{effect_id}", "session_id": "sess-unplaced",
                "trace_id": f"trace-{effect_id}", "root": entry_root, "path": path, "operation": "edit",
                "intent": "workspace.write_file", "tool": "workspace__write_file", "provider": "qwen-local",
            }
            if kind == "effect_intended":
                entry["intended"] = {"after_sha256": "ef" * 32}
            else:
                entry["outcome"] = "succeeded"
            blackbox.append(entry)
    return root


def _effects(out: dict) -> set[str]:
    return {item["effect_id"] for item in out["results"] if item["store"] == "blackbox"}


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_an_entry_under_a_root_not_on_this_machine_is_not_recoverable_and_names_the_root(ws: Path, absolute) -> None:
    query = str(ws / "lost" / "gone.txt") if absolute else "lost/gone.txt"

    out = pa.recovery_plan(path=query, workspace_root=str(ws))

    assert _effects(out) == set(), out["results"]
    assert out["plan"]["recoverable"] is False
    notes = out["plan"]["notes"]
    assert any("NOT RECOVERABLE" in note and GONE_ROOT in note for note in notes), notes


def test_the_command_names_the_root_too(ws: Path) -> None:
    envelope = execute_command(
        "archaeology.recovery_plan", {"path": "lost/gone.txt", "workspace_root": str(ws)},
        context=ExecutionContext(projection="cli"),
    )

    assert envelope.ok is True, envelope
    assert "NOT RECOVERABLE" in envelope.summary, envelope.summary
    assert any(GONE_ROOT in note for note in envelope.data["plan"]["notes"]), envelope.data["plan"]


def test_an_entry_under_a_root_on_this_machine_still_answers(ws: Path) -> None:
    out = pa.recovery_plan(path="kept/k.txt", workspace_root=str(ws))

    assert _effects(out) == {"eff-kept-02"}, out["results"]
    assert out["plan"]["recoverable"] is True
    assert not any(GONE_ROOT in note for note in out["plan"]["notes"])


def test_trace_of_a_path_skips_an_entry_under_an_unplaced_root(ws: Path) -> None:
    out = pa.trace(path="lost/gone.txt", workspace_root=str(ws))

    assert _effects(out) == set(), out["results"]


def test_a_relative_path_with_no_workspace_root_matches_nothing(ws: Path) -> None:
    out = pa.trace(path="kept/k.txt")

    assert _effects(out) == set(), out["results"]
