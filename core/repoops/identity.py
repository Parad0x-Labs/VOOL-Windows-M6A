"""RepositoryWorkspace identity: WHICH checkout of WHICH repo, exactly.

A workspace is a PAIR: the local checkout (absolute root path + its git truth)
and the remote repository it tracks (full RepoRef from core.remote_forge,
including numeric id and fork parent). Neither half is derivable from the
other: two checkouts of one repo are different workspaces, and a renamed or
transferred repo is a different RepoRef even at the same URL.

No fuzzy names. Every downstream operation carries the workspace identity and
refuses on mismatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.remote_forge.identity import RepoIdentity


class WorkspaceError(RuntimeError):
    pass


def _run(root: str, *args: str) -> tuple[int, str, str]:
    """One git call in ``root`` through the door the rest of repoops uses
    (core/repoops/gitops.py ``GitRunner.run`` -> core/safe_git.py): a repository
    whose own config names programs git would run is refused before git starts,
    and what does run is hardened with a scrubbed environment. A refusal reports
    itself as ``(-1, "", "git was not run: <reason>")``."""
    from core.repoops.gitops import GitRunner

    return GitRunner(root=Path(root)).run(*args)


def _git(root: str, *args: str) -> str:
    code, out, err = _run(root, *args)
    if code != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {err.strip()}")
    return out.strip()


@dataclass(frozen=True)
class LocalState:
    """Git's own report of this checkout — never inferred."""

    head_sha: str
    branch: str | None          # None == detached HEAD
    dirty: bool
    changed_paths: tuple[str, ...] = ()

    @property
    def detached(self) -> bool:
        return self.branch is None


@dataclass(frozen=True)
class RepositoryWorkspace:
    """The exact pairing of local checkout to remote repository identity."""

    root: str                    # absolute, resolved path — never relative
    repo: RepoIdentity | None = None   # None until attached/cloned/created
    upstream_branch: str = ""    # e.g. 'origin/main'; empty when untracked

    @property
    def key(self) -> str:
        repo_key = self.repo.key() if self.repo is not None else "unbound"
        return f"{repo_key}@{self.root}"

    def read_local_state(self) -> LocalState:
        sha = _git(self.root, "rev-parse", "HEAD")
        # Exit 1 is detached HEAD's normal answer; a refusal (-1) is an error.
        code, out, err = _run(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if code == 1:
            symbolic = ""        # detached: exit 1, no output
        elif code != 0:
            raise WorkspaceError(f"symbolic-ref failed: {err.strip()}")
        else:
            symbolic = out.strip()
        status = _git(self.root, "status", "--porcelain")
        paths = tuple(sorted(
            line.split(None, 1)[1] for line in status.splitlines() if line.strip()
        ))
        return LocalState(
            head_sha=sha,
            branch=symbolic or None,
            dirty=bool(paths),
            changed_paths=paths,
        )

    def require_same_repo(self, other: RepositoryWorkspace) -> None:
        """A second similar checkout is a DIFFERENT workspace; refuse substitution."""
        if self.key != other.key:
            raise WorkspaceError(
                f"workspace mismatch: bound to {self.key}, got {other.key}"
            )
