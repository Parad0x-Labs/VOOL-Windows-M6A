"""On Windows, an archaeology read that needed git answers with the git limit, never "not found".

VOOL runs no git on Windows in 0.7.1 (core/safe_git.py). The archaeology git reader is refused before
any process starts, and the refusal was kept only in ``limits.truncation_reasons``. ``locate`` then found
nothing else and raised ``ReferenceNotFound``, so the chat read "not found: no in-scope object matches
reference '<sha>'" for a commit VOOL never looked for. ``recovery_plan`` said "nothing matching the
reference was found in any in-scope store" in the same case.

Windows is simulated the way the other git-refused tests do it: ``safe_git._REFUSE_GIT = True``. The
repository is built with the real git first (as on Linux), and every row then proves that no git process
is started. Off Windows, and on Windows when no workspace root brings git into scope, "not found" stays
the answer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import core.project_archaeology as pa
from core import safe_git
from core.command_registry.execute import ExecutionContext, execute_command


@pytest.fixture()
def repo(arch_home, blackbox, git_repo) -> dict:
    return git_repo


@pytest.fixture()
def windows(repo, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", True)


@pytest.fixture()
def no_git_starts(windows, monkeypatch) -> list:
    """Every attempt to start git through subprocess; the attempt itself fails the row."""
    calls: list = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            text = " ".join(str(part) for part in args) if isinstance(args, (list, tuple)) else str(args)
            if "git" in Path(text.split(" ", 1)[0]).name.lower():
                calls.append(args)
                raise AssertionError(f"git was started on Windows: {args!r}")
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return calls


def _is_the_git_limit(exc: BaseException) -> bool:
    return str(exc) == safe_git.GIT_REFUSED_TEXT and not isinstance(exc, pa.ReferenceNotFound)


@pytest.mark.parametrize("width", [40, 12, 8, 7])
def test_locate_of_a_commit_sha_answers_the_git_limit_not_not_found(repo, no_git_starts, width) -> None:
    sha = repo["sha_b"][:width]

    with pytest.raises(pa.ArchaeologyInputError) as raised:
        pa.locate(sha, workspace_root=str(repo["repo"]))

    assert _is_the_git_limit(raised.value), repr(raised.value)
    assert no_git_starts == []


def test_locate_that_matches_nothing_while_git_was_refused_answers_the_git_limit(repo, no_git_starts) -> None:
    with pytest.raises(pa.ArchaeologyInputError) as raised:
        pa.locate("no-such-object-xyz-000", workspace_root=str(repo["repo"]))

    assert _is_the_git_limit(raised.value), repr(raised.value)
    assert no_git_starts == []


def test_locate_that_finds_a_sha_elsewhere_while_git_was_refused_says_git_was_not_read(
    repo, no_git_starts, monkeypatch
) -> None:
    # Another store holds an object whose id is the sha: it is answered, and the answer says the git
    # store was not read, because a git object of that name may exist too.
    sha = repo["sha_b"]

    def honesty_items(*, scan, filters, deadline):
        item = pa.make_item(store="honesty_receipts", object_id=sha, kind="receipt", confidence="exact")
        return [item], {"store": "honesty_receipts", "root": "", "entries": 1, "verified": True}

    monkeypatch.setattr(pa, "_honesty_items", honesty_items)

    out = pa.locate(sha, workspace_root=str(repo["repo"]))

    assert [item["store"] for item in out["results"]] == ["honesty_receipts"], out["results"]
    assert out["notices"] == [safe_git.GIT_REFUSED_TEXT], out
    assert no_git_starts == []


def test_locate_that_finds_a_non_sha_reference_while_git_was_refused_carries_no_git_notice(
    repo, no_git_starts
) -> None:
    out = pa.locate("eff-push-77", workspace_root=str(repo["repo"]))

    assert out["results"], out
    assert out.get("notices", []) == [], out
    assert no_git_starts == []


def test_the_locate_command_reads_the_git_limit_sentence(repo, no_git_starts) -> None:
    envelope = execute_command(
        "archaeology.locate",
        {"reference": repo["sha_b"], "workspace_root": str(repo["repo"])},
        context=ExecutionContext(projection="cli"),
    )

    assert envelope.ok is False
    assert envelope.summary == safe_git.GIT_REFUSED_TEXT, envelope.summary
    assert "not found" not in envelope.summary
    assert (envelope.fault.detail or {}).get("status") == safe_git.GIT_REFUSED_STATUS, envelope.fault
    assert no_git_starts == []


def test_recovery_plan_by_sha_says_git_was_not_read_instead_of_nothing_found(repo, no_git_starts) -> None:
    out = pa.recovery_plan(reference=repo["sha_b"], workspace_root=str(repo["repo"]))

    notes = out["plan"]["notes"]
    assert safe_git.GIT_REFUSED_TEXT in notes, notes
    assert not any("was found in any in-scope store" in note for note in notes), notes
    assert out["plan"]["recoverable"] is False
    assert out["notices"] == [safe_git.GIT_REFUSED_TEXT], out
    assert no_git_starts == []


def test_recovery_plan_that_matches_nothing_says_git_was_not_read(repo, no_git_starts) -> None:
    out = pa.recovery_plan(reference="no-such-object-xyz-000", workspace_root=str(repo["repo"]))

    notes = out["plan"]["notes"]
    assert out["results"] == [], out["results"]
    assert safe_git.GIT_REFUSED_TEXT in notes, notes
    assert not any("was found in any in-scope store" in note for note in notes), notes
    assert no_git_starts == []


def test_the_recovery_plan_command_reads_the_git_limit_sentence(repo, no_git_starts) -> None:
    envelope = execute_command(
        "archaeology.recovery_plan",
        {"reference": repo["sha_b"], "workspace_root": str(repo["repo"])},
        context=ExecutionContext(projection="cli"),
    )

    assert envelope.ok is True, envelope
    assert safe_git.GIT_REFUSED_TEXT in envelope.summary, envelope.summary
    assert no_git_starts == []


# -- guards: the answers that were already honest stay as they are -----------------------------------


def test_compare_of_commits_on_windows_already_answers_the_git_limit(repo, no_git_starts) -> None:
    with pytest.raises(pa.ArchaeologyInputError) as raised:
        pa.compare(kind="git_commits", a=repo["sha_a"], b=repo["sha_b"], workspace_root=str(repo["repo"]))

    assert str(raised.value) == safe_git.GIT_REFUSED_TEXT
    assert no_git_starts == []


def test_on_windows_without_a_workspace_root_an_unknown_reference_is_still_not_found(repo, no_git_starts) -> None:
    # No workspace root: no store VOOL would read needs git, on any platform, so "not found" is true.
    with pytest.raises(pa.ReferenceNotFound):
        pa.locate(repo["sha_b"])
    assert no_git_starts == []


def test_off_windows_an_unknown_reference_is_still_not_found(repo, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)

    with pytest.raises(pa.ReferenceNotFound):
        pa.locate("no-such-object-xyz-000", workspace_root=str(repo["repo"]))
    out = pa.recovery_plan(reference="no-such-object-xyz-000", workspace_root=str(repo["repo"]))
    assert "nothing matching the reference was found in any in-scope store" in out["plan"]["notes"]
    assert out.get("notices", []) == [], out


def test_off_windows_locate_still_finds_the_commit(repo, monkeypatch) -> None:
    monkeypatch.setattr(safe_git, "_REFUSE_GIT", False)

    out = pa.locate(repo["sha_b"], workspace_root=str(repo["repo"]))

    assert any(item["store"] == "git" and item["hash"] == repo["sha_b"] for item in out["results"]), out
    assert out.get("notices", []) == [], out
