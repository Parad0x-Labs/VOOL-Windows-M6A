"""LocalGit: typed git operations. Observation and mutation stay distinct.

The load-bearing rule: `git pull` does NOT exist here as one opaque operation.
A sync is FETCH (observation) → compare LOCAL_HEAD vs FETCHED_REMOTE_HEAD →
classify fast-forward / diverged → PROPOSE the local mutation (merge or
rebase) → authorize through the platform broker → apply. A model can never
accidentally merge remote state into a dirty tree by saying "pull".

Mutating operations are broker-admitted with derived idempotency keys; reads
run directly (read-only, no admission needed beyond workspace binding).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from core.platform.broker import EffectOutcome, EffectRequest, ExecutionBroker
from core.repoops.identity import LocalState, RepositoryWorkspace, WorkspaceError, _git


@dataclass(frozen=True)
class CommitRecord:
    sha: str
    message: str
    files: tuple[str, ...]
    diff_hash: str              # hash over the staged diff — binds message to change


@dataclass(frozen=True)
class FetchComparison:
    """The honest divergence statement after a fetch — no mutation implied."""

    local_head: str
    fetched_remote_head: str
    relation: str               # in_sync | local_behind_fast_forward | diverged | unknown
    fetched_at_sha_bound: bool = True


def classify_sync(local_head: str, fetched_remote_head: str,
                  *, is_ancestor_fn=None) -> FetchComparison:
    """Compare two heads WITHOUT mutating anything."""
    if local_head == fetched_remote_head:
        return FetchComparison(local_head, fetched_remote_head, "in_sync")
    if is_ancestor_fn is not None and is_ancestor_fn(local_head, fetched_remote_head):
        return FetchComparison(local_head, fetched_remote_head, "local_behind_fast_forward")
    return FetchComparison(local_head, fetched_remote_head, "diverged")


class LocalGit:
    """Typed operations for ONE RepositoryWorkspace."""

    def __init__(self, workspace: RepositoryWorkspace) -> None:
        self._ws = workspace

    # -- reads ---------------------------------------------------------------------
    def status(self) -> LocalState:
        return self._ws.read_local_state()

    def log(self, limit: int = 10) -> tuple[tuple[str, str], ...]:
        out = _git(self._ws.root, "log", f"-{limit}", "--format=%H %s")
        lines = [tuple(line.split(" ", 1)) for line in out.splitlines() if line.strip()]
        return tuple((sha, subject) for sha, subject in lines)  # type: ignore[misc]

    def diff(self, *, staged: bool = False) -> str:
        args = ["diff", "--cached"] if staged else ["diff"]
        return _git(self._ws.root, *args)

    def blame(self, rel_path: str) -> str:
        return _git(self._ws.root, "blame", "--", rel_path)

    def branches(self) -> tuple[str, ...]:
        return tuple(
            line.removeprefix("* ").strip()
            for line in _git(self._ws.root, "branch", "--format=%(refname:short)").splitlines()
            if line.strip()
        )

    def remotes(self) -> dict[str, str]:
        out = _git(self._ws.root, "remote", "-v")
        remotes: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "(fetch)" in line:
                remotes[parts[0]] = parts[1]
        return remotes

    def fetch(self) -> str:
        """OBSERVATION ONLY: update remote-tracking refs; never touches HEAD."""
        _git(self._ws.root, "fetch", "--all", "--quiet")
        return self.fetched_remote_head()

    def fetched_remote_head(self) -> str:
        upstream = self._ws.upstream_branch or "@{upstream}"
        try:
            return _git(self._ws.root, "rev-parse", upstream)
        except WorkspaceError:
            return ""

    def is_ancestor(self, older: str, newer: str) -> bool:
        from core.repoops.gitops import GitRunner

        code, _, err = GitRunner(root=Path(self._ws.root)).run(
            "merge-base", "--is-ancestor", older, newer)
        if code == -1:
            # A refused repository must not be read as "histories diverged".
            raise WorkspaceError(str(err))
        return code == 0

    def compare_after_fetch(self) -> FetchComparison:
        """FETCH then compare — still pure observation."""
        remote_head = self.fetch()
        local = self.status()
        return classify_sync(local.head_sha, remote_head,
                             is_ancestor_fn=self.is_ancestor)

    # -- mutations (broker-admitted) -------------------------------------------------
    def _admit(self, fork, broker: ExecutionBroker, op: str, token: str,
               params: dict[str, object], dispatch):
        request = EffectRequest(
            effect_id=f"git.{op}", required_capability=token,
            params={"workspace": self._ws.key, **params},
            idempotency_key=hashlib.sha256(
                f"{op}:{json.dumps(params, sort_keys=True)}:{self._ws.key}".encode()
            ).hexdigest()[:32],
        )
        return broker.execute(fork, request, dispatch)

    def create_branch_and_switch(self, fork, broker: ExecutionBroker,
                                 branch: str) -> object:
        def dispatch() -> EffectOutcome:
            _git(self._ws.root, "switch", "-c", branch)
            return EffectOutcome(status="applied",
                                 evidence={"branch": branch,
                                           "from": self.status().head_sha})

        return self._admit(fork, broker, "create_branch", "git.create_branch",
                           {"branch": branch}, dispatch)

    def commit_staged(self, fork, broker: ExecutionBroker, message: str) -> object:
        """Commit what is STAGED NOW. Evidence carries the resulting SHA + diff
        hash, so 'committed' is provable and bound to the exact change."""
        staged = _git(self._ws.root, "diff", "--cached", "--name-only")
        files = tuple(f for f in staged.splitlines() if f.strip())
        diff_hash = hashlib.sha256(
            _git(self._ws.root, "diff", "--cached").encode()
        ).hexdigest()[:16]

        def dispatch() -> EffectOutcome:
            from core.repoops.gitops import GitRunner
            from core.user_rules import git_outbound_refusal

            ruled = git_outbound_refusal(self._ws.root, "commit", message=message, staged=True)
            if ruled is not None:
                return EffectOutcome(status="refused", reason=ruled.reason)
            code, _, err = GitRunner(root=Path(self._ws.root)).run("commit", "-m", message)
            if code == -1 and str(err).startswith("git was not run:"):
                # Refused before git started: nothing ran, nothing ambiguous.
                return EffectOutcome(status="refused", reason=str(err).strip()[:200])
            if code == -1:
                # A timeout or a missing git: the commit may have landed, so
                # only "unknown" is honest (a raised exception lands there too).
                return EffectOutcome(status="unknown", reason=str(err).strip()[:200])
            if code != 0:
                return EffectOutcome(status="failed",
                                     reason=str(err).strip()[:200])
            sha = _git(self._ws.root, "rev-parse", "HEAD")
            return EffectOutcome(status="applied", evidence={
                "sha": sha, "message": message,
                "files": list(files), "diff_hash": diff_hash,
            })

        return self._admit(fork, broker, "commit", "git.commit",
                           {"message": message}, dispatch)


__all__ = [
    "CommitRecord", "FetchComparison", "LocalGit", "classify_sync",
]
