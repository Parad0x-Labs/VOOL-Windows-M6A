"""The model's git evidence stays inside the folder its chat is bound to.

Archaeology finds the git repository for ``workspace_root`` by walking up until it meets a
``.git``. With the chat bound to a subfolder of a larger repository, that walk left the bound
folder: the model saw commit subjects and the names of files changed anywhere in the larger
repository. For the model the walk now stops at the bound folder; an operator's walk is unchanged.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

OUTSIDE_SUBJECT = "ARCH-PARENT-SUBJECT-MARKER-77c1"
OUTSIDE_NAME = "parent-only-ledger.txt"

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    ).stdout.strip()


@pytest.fixture()
def parent_repo(arch_home) -> dict:
    """A repository whose subfolder ``bound`` is what the chat is bound to."""
    outer = arch_home["tmp"] / "outer"
    bound = outer / "bound"
    bound.mkdir(parents=True)
    _git(outer, "init", "-q")
    (bound / "notes.md").write_text("bound notes\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-q", "-m", "bound notes")
    first = _git(outer, "rev-parse", "HEAD")
    (outer / OUTSIDE_NAME).write_text("outside the bound folder\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-q", "-m", OUTSIDE_SUBJECT)
    second = _git(outer, "rev-parse", "HEAD")
    return {"outer": outer, "bound": bound, "first": first, "second": second}


def _model_call(command: str, arguments: dict, *, bound: Path):
    from core import tool_intent_executor as executor
    from core.command_registry.model_tools import project_model_tools
    from core.hive_activity_tracker import HiveActivityTracker, HiveActivityTrackerConfig

    project_model_tools()
    return executor.execute_tool_intent(
        {"intent": f"operator.command.archaeology.{command}", "arguments": dict(arguments)},
        task_id=f"arch-parent-{uuid.uuid4().hex}",
        session_id="sess-parent",
        checkpoint_id=f"runtime-{uuid.uuid4().hex}",
        step_index=0,
        source_context={"operating_mode": "auto", "workspace": str(bound)},
        hive_activity_tracker=HiveActivityTracker(
            config=HiveActivityTrackerConfig(enabled=False, watcher_api_url=None)
        ),
    )


def _envelope(execution) -> dict:
    return execution.details["envelope"]


def test_model_compare_never_reads_a_repository_above_the_bound_folder(parent_repo):
    execution = _model_call(
        "compare",
        {"kind": "git_commits", "a": parent_repo["first"], "b": parent_repo["second"],
         "workspace_root": str(parent_repo["bound"])},
        bound=parent_repo["bound"],
    )
    envelope = _envelope(execution)
    assert execution.ok is False, envelope
    assert OUTSIDE_NAME not in repr(envelope), envelope
    assert OUTSIDE_SUBJECT not in repr(envelope), envelope


def test_model_locate_never_returns_a_commit_subject_from_above_the_bound_folder(parent_repo):
    execution = _model_call(
        "locate",
        {"reference": parent_repo["second"], "workspace_root": str(parent_repo["bound"])},
        bound=parent_repo["bound"],
    )
    assert OUTSIDE_SUBJECT not in repr(_envelope(execution)), _envelope(execution)


# -- guards --------------------------------------------------------------------------


def test_model_still_reads_a_repository_rooted_inside_the_bound_folder(parent_repo):
    # Bound to the repository itself, a subfolder of it may still be named.
    execution = _model_call(
        "compare",
        {"kind": "git_commits", "a": parent_repo["first"], "b": parent_repo["second"],
         "workspace_root": str(parent_repo["bound"])},
        bound=parent_repo["outer"],
    )
    envelope = _envelope(execution)
    assert execution.ok is True, envelope
    assert OUTSIDE_NAME in envelope["data"]["comparison"]["changed_paths"], envelope


def test_operator_compare_still_walks_up_to_the_repository(parent_repo):
    import core.project_archaeology as pa

    result = pa.compare(
        kind="git_commits", a=parent_repo["first"], b=parent_repo["second"],
        workspace_root=str(parent_repo["bound"]),
    )
    assert OUTSIDE_NAME in result["comparison"]["changed_paths"], result["comparison"]


@pytest.mark.parametrize("pointer", ["gitdir-file", "link"])
def test_model_never_follows_a_dot_git_pointer_out_of_the_bound_folder(pointer, arch_home):
    # A `.git` entry inside the bound folder can lead to a repository anywhere: a `gitdir:` file
    # or a link. Git follows it, so where it leads must be inside the bound folder too.
    secret = arch_home["tmp"] / "secret-repo"
    secret.mkdir()
    _git(secret, "init", "-q")
    (secret / OUTSIDE_NAME).write_text("1\n", encoding="utf-8")
    _git(secret, "add", ".")
    _git(secret, "commit", "-q", "-m", "first")
    first = _git(secret, "rev-parse", "HEAD")
    (secret / OUTSIDE_NAME).write_text("2\n", encoding="utf-8")
    _git(secret, "commit", "-q", "-am", OUTSIDE_SUBJECT)
    second = _git(secret, "rev-parse", "HEAD")
    bound = arch_home["tmp"] / "bound"
    bound.mkdir()
    if pointer == "gitdir-file":
        (bound / ".git").write_text(f"gitdir: {secret / '.git'}\n", encoding="utf-8")
    else:
        os.symlink(secret / ".git", bound / ".git", target_is_directory=True)

    compared = _model_call(
        "compare",
        {"kind": "git_commits", "a": first, "b": second, "workspace_root": str(bound)},
        bound=bound,
    )
    located = _model_call(
        "locate", {"reference": second, "workspace_root": str(bound)}, bound=bound
    )

    assert compared.ok is False, _envelope(compared)
    for execution in (compared, located):
        assert OUTSIDE_NAME not in repr(_envelope(execution)), _envelope(execution)
        assert OUTSIDE_SUBJECT not in repr(_envelope(execution)), _envelope(execution)
