"""The repoops helpers that predate the safe_git door ran git raw.

core/repoops/identity.py, archaeology.py, localgit.py and bootstrap.py issued
plain `git -C <root> ...` subprocesses with the ambient environment: a
repository's core.fsmonitor ran on their `git status` and `git diff`, a local
clone served the source's upload-pack under the source's own config, and a
commit ran the repository's own hooks. Each of those calls now goes through
the door the rest of repoops uses (core/repoops/gitops.py GitRunner.run ->
core/safe_git.py): a repository whose own config names programs git would run
is refused before git starts.

Each planted program writes a marker file; the marker and the repository's
HEAD are read back from disk, whatever the helper reports. No POSIX skip: on
Windows the planted programs are .cmd files, and the refusal is asserted by
type, never by whether this platform's git would have spawned the program.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.platform.broker import ExecutionBroker, PlatformRevocations
from core.remote_forge.identity import IdentityError
from core.repoops.archaeology import (
    FakeCapabilityAdapter,
    ReproductionNeed,
    collect_archaeology,
)
from core.repoops.bootstrap import BootstrapError, attach, clone, init_local_repo
from core.repoops.identity import RepositoryWorkspace, WorkspaceError
from core.repoops.localgit import LocalGit
from tests.repoops._harness import git, head


def _program(folder: Path, marker: Path) -> str:
    """A program git can run on this platform; writes ``marker`` when it runs."""
    if os.name == "nt":
        program = folder / "program.cmd"
        program.write_text(f'@echo ran > "{marker}"\r\n', encoding="utf-8")
        return str(program)
    program = folder / "program.sh"
    program.write_text(f"#!/bin/sh\necho ran > '{marker}'\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return str(program)


def _hook(path: Path, marker: Path) -> None:
    """A git hook (git runs hooks through sh, shebang first line included)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho ran > '{marker}'\nexit 0\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "n@x")
    git(root, "config", "user.name", "Ninja")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "second")
    return root


def _name_a_program(repo: Path, tmp_path: Path, key: str) -> Path:
    marker = tmp_path / "PROGRAM_RAN"
    git(repo, "config", key, _program(tmp_path, marker))
    return marker


# ------------------------------------------------- refused: config names a program


def test_a_workspace_status_refuses_a_config_that_names_a_program(
        repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "core.fsmonitor")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="git was not run"):
        RepositoryWorkspace(root=str(repo)).read_local_state()

    assert not marker.exists()


def test_archaeology_refuses_the_same_config(repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "core.fsmonitor")

    with pytest.raises(WorkspaceError, match="git was not run"):
        collect_archaeology(RepositoryWorkspace(root=str(repo)), None)

    assert not marker.exists()


def test_the_reproduction_adapter_refuses_the_same_config(
        repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "core.fsmonitor")
    need = ReproductionNeed(capability="python.test.pytest", cwd=str(repo),
                            target="tests/absent.py::test_absent",
                            runtime_constraints={})

    with pytest.raises(WorkspaceError, match="git was not run"):
        FakeCapabilityAdapter().resolve_and_run(need)

    assert not marker.exists()


def test_is_ancestor_refuses_the_config_instead_of_answering_false(
        repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "core.fsmonitor")
    older = git(repo, "rev-parse", "HEAD~1").strip()

    with pytest.raises(WorkspaceError, match="git was not run"):
        LocalGit(RepositoryWorkspace(root=str(repo))).is_ancestor(older, head(repo))

    assert not marker.exists()


def test_attach_refuses_a_checkout_whose_config_names_a_program(
        repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "core.fsmonitor")
    git(repo, "remote", "add", "origin", "https://github.com/acme/api.git")

    with pytest.raises(BootstrapError, match="git was not run"):
        attach(str(repo))

    assert not marker.exists()


def test_clone_refuses_a_local_source_whose_config_names_a_program(
        repo: Path, tmp_path: Path) -> None:
    marker = _name_a_program(repo, tmp_path, "uploadpack.packObjectsHook")
    dest = tmp_path / "clone-of-it"

    with pytest.raises(BootstrapError, match="names programs git would run"):
        clone(str(repo), str(dest))

    assert not marker.exists()
    assert not dest.exists()


# ------------------------------------------------- refused: hooks and their friends


def test_a_commit_through_localgit_never_runs_a_planted_hook(
        repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "HOOK_RAN"
    _hook(repo / ".git" / "hooks" / "pre-commit", marker)
    (repo / "f.txt").write_text("data\n", encoding="utf-8")
    git(repo, "add", "f.txt")
    before = head(repo)

    receipt = LocalGit(RepositoryWorkspace(root=str(repo))).commit_staged(
        PlatformRevocations().mint("ninja", ["git.commit"]), ExecutionBroker(), "add f")

    assert receipt.status == "refused", receipt
    assert "git was not run" in (receipt.reason or "")
    assert not marker.exists()
    assert head(repo) == before


# ------------------------------------------------- ordinary repositories still work


def test_the_helpers_still_work_on_an_ordinary_repository(repo: Path) -> None:
    ws = RepositoryWorkspace(root=str(repo))

    state = ws.read_local_state()
    assert state.head_sha == head(repo)
    assert state.branch == "main" and not state.dirty

    facts = collect_archaeology(ws, None)
    assert facts.workspace_key == ws.key
    assert facts.changed_files == ("b.txt",)          # exactly the second commit

    lg = LocalGit(ws)
    older = git(repo, "rev-parse", "HEAD~1").strip()
    assert lg.is_ancestor(older, head(repo)) is True
    assert lg.is_ancestor(head(repo), older) is False
    assert len(lg.log()) == 2

    (repo / "c.txt").write_text("three\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    receipt = lg.commit_staged(
        PlatformRevocations().mint("ninja", ["git.commit"]), ExecutionBroker(), "add c")
    assert receipt.status == "applied", receipt
    assert receipt.evidence["files"] == ["c.txt"]
    assert receipt.evidence["sha"] == head(repo)


def test_attach_still_works_on_an_ordinary_checkout(tmp_path: Path) -> None:
    ws = init_local_repo(str(tmp_path / "fresh"))
    root = Path(ws.root)
    git(root, "config", "user.email", "n@x")
    git(root, "config", "user.name", "Ninja")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "remote", "add", "origin", "https://github.com/acme/api.git")

    attached, remotes = attach(str(root))

    assert attached.root == ws.root
    assert remotes == {"origin": "https://github.com/acme/api.git"}
    assert attached.repo.slug() == "acme/api"


def test_clone_still_clones_an_ordinary_local_source(
        repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "clone-dst"

    with pytest.raises(IdentityError):
        # A local path is no forge URL, so naming the clone fails after git has
        # already cloned (the behaviour tests/repoops/test_bootstrap.py pins).
        clone(str(repo), str(dest))

    assert (dest / ".git" / "HEAD").exists()
    assert head(dest) == head(repo)


def test_init_still_refuses_to_nest_inside_an_existing_repository(
        tmp_path: Path) -> None:
    outer = init_local_repo(str(tmp_path / "outer"))

    with pytest.raises(BootstrapError, match="inside existing repository"):
        init_local_repo(str(Path(outer.root) / "sub"))
