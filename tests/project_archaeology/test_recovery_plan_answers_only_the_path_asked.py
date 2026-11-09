"""recovery_plan for a path answers only from evidence about that path.

The journal reader ignored the queried path: `recovery_plan(path=...)` returned every journal entry,
and the plan then called a path the journal never touched RECOVERABLE, from an unrelated effect. The
memory reader likewise answered a path-only query with every memory item, which no path can match.

Rule (all platforms): an entry answers a path query only when its canonical path equals the queried
path, or lies inside it for a folder query. Both sides are compared after realpath (so a symbolic link
or junction is followed), and with normcase on Windows (case-insensitive there). A sibling that only
shares a prefix (`a/b` and `a/bc`) never matches. With no match, the plan says plainly NOT RECOVERABLE.

Windows-only behaviour (case folding, drive paths) is pinned through the comparison helper with
`ntpath`, since Linux cannot run the Windows path functions for real.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path

import pytest

import core.project_archaeology as pa
from core.command_registry.execute import ExecutionContext, execute_command


@pytest.fixture()
def ws(arch_home, blackbox, memory) -> Path:
    """A real workspace whose journal records effects on notes/a.txt and a/bc/x.txt (the corpus's own
    entries, recorded under a root that does not exist here, stay in the journal beside them)."""
    root = (arch_home["tmp"] / "real-ws").resolve()
    (root / "notes").mkdir(parents=True)
    (root / "a" / "bc").mkdir(parents=True)
    (root / "notes" / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "a" / "bc" / "x.txt").write_text("x\n", encoding="utf-8")
    for effect_id, path in (("eff-exact-01", "notes/a.txt"), ("eff-sibling-02", "a/bc/x.txt")):
        for kind in ("effect_intended", "effect_terminal"):
            entry = {
                "schema": "blackbox_effect_v1", "kind": kind,
                "effect_id": effect_id, "turn_id": f"turn-{effect_id}", "session_id": "sess-exact",
                "trace_id": f"trace-{effect_id}", "root": str(root), "path": path, "operation": "edit",
                "intent": "workspace.write_file", "tool": "workspace__write_file", "provider": "qwen-local",
            }
            if kind == "effect_intended":
                entry["intended"] = {"after_sha256": "cd" * 32}
            else:
                entry["outcome"] = "succeeded"
            blackbox.append(entry)
    return root


def _effects(out: dict) -> set[str]:
    return {item["effect_id"] for item in out["results"] if item["store"] == "blackbox"}


def _not_recoverable(out: dict) -> None:
    plan = out["plan"]
    assert plan["recoverable"] is False, plan
    assert plan["steps"] == [], plan
    assert any("NOT RECOVERABLE" in note for note in plan["notes"]), plan["notes"]
    assert [item for item in out["results"] if item["store"] in ("blackbox", "memory", "honesty_receipts")] == []


def test_an_untouched_path_is_not_recoverable(ws: Path) -> None:
    out = pa.recovery_plan(path="notes/never.txt", workspace_root=str(ws))

    _not_recoverable(out)


def test_the_command_says_not_recoverable_for_an_untouched_path(ws: Path) -> None:
    envelope = execute_command(
        "archaeology.recovery_plan", {"path": "notes/never.txt", "workspace_root": str(ws)},
        context=ExecutionContext(projection="cli"),
    )

    assert envelope.ok is True, envelope
    assert "NOT RECOVERABLE" in envelope.summary, envelope.summary
    assert envelope.data["plan"]["recoverable"] is False


def test_the_exact_path_is_recoverable_from_its_own_effect(ws: Path) -> None:
    out = pa.recovery_plan(path="notes/a.txt", workspace_root=str(ws))

    assert _effects(out) == {"eff-exact-01"}, out["results"]
    assert out["plan"]["recoverable"] is True
    assert out["plan"]["steps"][0]["object_id"] == "eff-exact-01", out["plan"]["steps"]


def test_a_folder_query_answers_the_entries_inside_it(ws: Path) -> None:
    out = pa.recovery_plan(path="a/bc", workspace_root=str(ws))

    assert _effects(out) == {"eff-sibling-02"}, out["results"]
    assert out["plan"]["recoverable"] is True


@pytest.mark.parametrize("query", ["a/b", "a/b/x.txt", "notes/a.txt.bak", "notes/a", "note", "notes2/a.txt"])
def test_a_sibling_or_prefix_path_does_not_match(ws: Path, query: str) -> None:
    out = pa.recovery_plan(path=query, workspace_root=str(ws))

    _not_recoverable(out)


def test_an_absolute_path_through_a_linked_folder_matches_after_realpath(ws: Path) -> None:
    link = ws.parent / "ws-link"
    os.symlink(ws, link, target_is_directory=True)

    through_link = pa.recovery_plan(path=str(link / "notes" / "a.txt"), workspace_root=str(link))
    relative_in_link = pa.recovery_plan(path="notes/a.txt", workspace_root=str(link))

    for out in (through_link, relative_in_link):
        assert _effects(out) == {"eff-exact-01"}, out["results"]
        assert out["plan"]["recoverable"] is True


def test_a_linked_file_matches_the_file_it_points_to(ws: Path) -> None:
    os.symlink(ws / "notes" / "a.txt", ws / "notes" / "alias.txt")

    out = pa.recovery_plan(path="notes/alias.txt", workspace_root=str(ws))

    assert _effects(out) == {"eff-exact-01"}, out["results"]


def test_trace_of_a_path_joins_only_that_paths_effects(ws: Path) -> None:
    out = pa.trace(path="notes/a.txt", workspace_root=str(ws))

    assert _effects(out) == {"eff-exact-01"}, out["results"]


# -- the comparison itself, as each platform reads paths ---------------------------------------------


@pytest.mark.parametrize("query, recorded, expected", [
    ("C:\\a\\b", "C:\\a\\b", True),
    ("C:\\a\\b", "C:\\a\\b\\x.txt", True),
    ("C:\\a\\b", "C:\\a\\bc", False),
    ("C:\\a\\b", "C:\\a\\bc\\x.txt", False),
    ("C:\\a\\b\\", "C:\\a\\b\\x.txt", True),
    ("c:\\A\\B", "C:\\a\\b\\X.txt", True),
    ("C:/a/b/x.txt", "C:\\a\\b\\x.txt", True),
    ("C:\\a\\b.txt", "C:\\a\\b.txt.bak", False),
], ids=["same", "inside", "sibling", "inside-sibling", "trailing-separator", "case-only", "slashes", "suffix"])
def test_windows_paths_compare_case_insensitively_and_never_by_prefix(query, recorded, expected) -> None:
    assert pa._path_covers(query, recorded, pathmod=ntpath) is expected


@pytest.mark.parametrize("query, recorded, expected", [
    ("/w/a/b", "/w/a/b", True),
    ("/w/a/b", "/w/a/b/x.txt", True),
    ("/w/a/b", "/w/a/bc", False),
    ("/w/a/b", "/w/a/bc/x.txt", False),
    ("/w/a/B", "/w/a/b", False),
    ("/", "/w/a", True),
], ids=["same", "inside", "sibling", "inside-sibling", "case-differs", "root"])
def test_posix_paths_compare_exactly_and_never_by_prefix(query, recorded, expected) -> None:
    assert pa._path_covers(query, recorded, pathmod=posixpath) is expected
