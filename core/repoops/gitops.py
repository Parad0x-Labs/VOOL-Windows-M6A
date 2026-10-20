"""Typed local git operations for RepoOps.

This module runs git and reports what git said. It decides nothing: whether an operation MAY
run is settled above it by ``core.mode_permission_policy`` through the tool contract's declared
permission actions, and whether it is RECORDED is settled by the effect gateway and the flight
recorder at the door. What lives here is the part that is genuinely git's: which argv expresses
the operation, which exit conditions mean conflict rather than failure, and how to read the
result back as data instead of prose.

Two operations are DEFAULT-DENIED and are not reachable from this module at all -- there is no
force-push argv and no branch-delete argv here that a caller could reach without the runtime
first getting an explicit, non-ALLOW-by-default permission decision. That is deliberate: a
default-deny that lives only in an ``if`` above the call is one refactor away from being gone.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core import safe_git

#: A bounded git call. An unbounded git on a pathological repo hangs the tool call and the turn,
#: and the dispatch layer cannot cancel an in-flight subprocess.
GIT_TIMEOUT_SECONDS = 30

#: Operations this module knows how to express. `restore` is the conflict escape hatch: it aborts
#: an in-progress merge/cherry-pick/revert, which is the only safe generic recovery.
OPERATIONS = ("branch", "commit", "cherry_pick", "revert", "merge", "tag", "restore")

#: Named here so the refusal reads as a policy fact, not an omission.
DEFAULT_DENIED = {
    "force_push": "a force push rewrites published history; it needs an explicit operator grant",
    "branch_delete": "deleting a branch discards work that may exist nowhere else; it needs an explicit operator grant",
}

#: Subcommands that only read. Anything else may run hooks, so it is refused when one would run.
_READ_ONLY_SUBCOMMANDS = frozenset({
    "rev-parse", "status", "diff", "log", "show", "merge-base", "for-each-ref", "ls-files",
    "cat-file", "rev-list", "remote", "describe", "symbolic-ref",
})


@dataclass(frozen=True)
class GitResult:
    operation: str
    ok: bool
    status: str
    argv: tuple[str, ...]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    head: str = ""
    conflicted_paths: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "ok": self.ok,
            "status": self.status,
            "command": " ".join(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "head": self.head,
            "conflicted_paths": list(self.conflicted_paths),
            "detail": self.detail,
        }


@dataclass
class GitRunner:
    """Every git invocation RepoOps makes, in one place, bounded and captured."""

    root: Path
    timeout: int = GIT_TIMEOUT_SECONDS
    log: list[dict[str, object]] = field(default_factory=list)
    #: The chat/project whose user rules bind this runner's outbound operations
    #: (``core.user_rules.scope_keys_for``). Empty keys still apply global and folder-project rules.
    rule_scope: dict[str, str] = field(default_factory=dict)
    #: Notes naming the user-rule exceptions that let an operation through ("Allowed by your exception ...").
    rule_notes: list[str] = field(default_factory=list)

    def _rules_refusal(self, operation: str, **content: object) -> GitResult | None:
        """Refuse an operation whose message, name or added content carries a word the user's
        rules ban from git. Nothing in this module runs the operation after a refusal. A word the
        chat's or project's exception lifted passes, and the exception is noted."""
        from core.user_rules import git_outbound_check

        verdict = git_outbound_check(
            self.root,
            operation,
            chat_id=str(self.rule_scope.get("chat") or ""),
            project_id=str(self.rule_scope.get("project") or ""),
            **content,
        )
        if verdict.refusal is None:
            if verdict.note and verdict.note not in self.rule_notes:
                self.rule_notes.append(verdict.note)
            return None
        return GitResult(operation, False, "refused_by_user_rule", (), head=self.head(), detail=verdict.refusal.reason)

    def _repository_refusal(self, *, mutating: bool) -> str:
        """Why git must not run in this repository at all ("" when it may). A repository's own
        config must not make git start programs, and a mutation must not run hooks unsandboxed
        (core/safe_git.py)."""
        try:
            keys = safe_git.executable_local_config(self.root, timeout=self.timeout)
            hooks = safe_git.active_hooks(self.root, timeout=self.timeout) if mutating else []
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return f"the repository's git configuration could not be checked ({type(exc).__name__})"
        if keys:
            return "the repository's own git config names programs git would run: " + ", ".join(keys)
        if hooks:
            return "this repository has git hooks VOOL would have to run unsandboxed: " + ", ".join(hooks)
        return ""

    def push_destination_refusal(self, remote: str) -> str:
        """Why a push to ``remote`` must not run ("" when it may).

        A push to a local folder or ``file://`` URL runs ``git receive-pack`` on this machine inside
        the destination repository, and that git drops this side's ``-c`` hardening: it runs the
        destination's hooks, programs its config names (``core.alternateRefsCommand``), and with
        ``receive.denyCurrentBranch=updateInstead`` rewrites its checked-out files. So a local
        destination is checked like this repository is, and a transport VOOL cannot read the
        destination of (a remote helper, an unknown scheme, an unusual ``file://`` form) is refused.
        A check that cannot finish refuses. On Windows this repository's own git folders are checked
        first (`_windows_push_source_refusal`), since every push writes there too."""
        try:
            source = _windows_push_source_refusal(self.root, timeout=self.timeout)
            if source:
                return source
            urls, problem = self._push_urls(remote)
            if problem:
                return problem
            for url in urls:
                transport = _push_transport(url)
                if transport == "network":
                    continue
                if transport != "local":
                    return (f"VOOL pushes only over https, ssh or git, or to a local folder; {_shown(url)!r} "
                            "is not one of those")
                refusal = self._local_destination_refusal(url)
                if refusal:
                    return refusal
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return f"the push destination could not be checked ({type(exc).__name__})"
        return ""

    def _push_urls(self, remote: str) -> tuple[list[str], str]:
        """The URLs ``git push <remote>`` would push to, rewrites applied, as git itself resolves them."""
        name = str(remote or "").strip()
        if not name or name.startswith("-"):
            return [], f"{name!r} is not a remote VOOL can push to"
        code, out, _ = self.run("remote", "get-url", "--push", "--all", name)
        if code != 0:
            # Not a configured remote, so git pushes to it as a URL or path. A throwaway remote holding
            # it as its url gets the same pushInsteadOf/insteadOf rewrites git would apply (get-url
            # ignores a remote defined with -c, `remote -v` lists it). Its name is new on every call,
            # so no config can have given it a pushurl of its own.
            probe_remote = f"vool-push-target-{uuid.uuid4().hex}"
            probe = subprocess.run(
                safe_git.hardened_argv(self.root, "-c", f"remote.{probe_remote}.url={name}", "remote", "-v"),
                capture_output=True, text=True, timeout=self.timeout, env=safe_git.safe_env(),
            )
            code = probe.returncode
            prefix, suffix = f"{probe_remote}\t", " (push)"
            out = "\n".join(line[len(prefix):-len(suffix)] for line in probe.stdout.splitlines()
                            if line.startswith(prefix) and line.endswith(suffix))
        urls = [line.strip() for line in (out or "").splitlines() if line.strip()]
        if code != 0 or not urls:
            return [], f"the push destination of {_shown(name)!r} could not be read"
        vcs = subprocess.run(
            safe_git.hardened_argv(self.root, "config", "--get", f"remote.{name}.vcs"),
            capture_output=True, text=True, timeout=self.timeout, env=safe_git.safe_env(),
        )
        if vcs.returncode == 0 and vcs.stdout.strip():
            return [], f"remote {_shown(name)!r} names a remote helper (its vcs setting), which VOOL does not run"
        return urls, ""

    def _local_destination_refusal(self, url: str) -> str:
        path = _local_push_path(url)
        if path is None:
            return f"VOOL cannot tell which folder {_shown(url)!r} names, so it does not push there"
        if not path.is_absolute():
            # push runs from the top of the work tree (the git folder of a bare repository), not from
            # wherever <root> sits inside it, and resolves a relative destination from there.
            path = self._push_base() / path
        git_dir = _receiving_git_dir(path, timeout=self.timeout)
        url = _shown(url)
        if git_dir is None:
            return f"{url!r} is not a git repository VOOL can check"
        outside = _windows_destination_refusal(url, git_dir, timeout=self.timeout)
        if outside:
            return outside
        try:
            keys = _destination_program_keys(git_dir, timeout=self.timeout)
            hooks = safe_git.active_hooks(git_dir, timeout=self.timeout)
            update = subprocess.run(
                ["git", "--git-dir", str(git_dir), "config", "--get", "receive.denycurrentbranch"],
                capture_output=True, text=True, timeout=self.timeout, env=safe_git.safe_env(),
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return f"the push destination {url!r} could not be checked ({type(exc).__name__})"
        if keys:
            return f"the push destination {url!r} has git config naming programs a push would run: " + ", ".join(keys)
        if hooks:
            return f"the push destination {url!r} has git hooks a push would run unsandboxed: " + ", ".join(hooks)
        if update.stdout.strip().lower() == "updateinstead":
            return (f"the push destination {url!r} sets receive.denyCurrentBranch=updateInstead, so a push "
                    "would rewrite its checked-out files")
        return ""

    def _push_base(self) -> Path:
        for option in ("--show-toplevel", "--absolute-git-dir"):
            probe = subprocess.run(safe_git.hardened_argv(self.root, "rev-parse", option), capture_output=True,
                                   text=True, timeout=self.timeout, env=safe_git.safe_env())
            if probe.returncode == 0 and probe.stdout.strip():
                return Path(probe.stdout.strip())
        raise RuntimeError("the repository's top folder could not be resolved")

    def run(self, *argv: str) -> tuple[int, str, str]:
        refusal = self._repository_refusal(mutating=bool(argv) and argv[0] not in _READ_ONLY_SUBCOMMANDS)
        if not refusal and argv and argv[0] == "push":
            target = [arg for arg in argv[1:] if not arg.startswith("-")]
            refusal = self.push_destination_refusal(target[0] if target else "")
            if "--no-recurse-submodules" not in argv:  # submodule remotes are destinations nothing checked
                argv = ("push", "--no-recurse-submodules", *argv[1:])
        if refusal:
            self.log.append({"argv": list(argv), "returncode": -1, "refused": True})
            return -1, "", f"git was not run: {refusal}"
        command = safe_git.hardened_argv(self.root, *argv)
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout,
                                       env=safe_git.safe_env())
        except subprocess.TimeoutExpired:
            self.log.append({"argv": list(argv), "returncode": -1, "timed_out": True})
            return -1, "", f"git {' '.join(argv[:2])} timed out after {self.timeout}s"
        except FileNotFoundError:
            return -1, "", "git is not installed on this machine"
        self.log.append({"argv": list(argv), "returncode": completed.returncode})
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    # -- observations -------------------------------------------------------------------

    def is_repo(self) -> bool:
        code, _, _ = self.run("rev-parse", "--git-dir")
        return code == 0

    def head(self) -> str:
        code, out, _ = self.run("rev-parse", "HEAD")
        return out.strip() if code == 0 else ""

    def branch(self) -> str:
        code, out, _ = self.run("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip() if code == 0 else ""

    def remote_url(self, remote: str = "origin") -> str:
        code, out, _ = self.run("remote", "get-url", remote)
        return out.strip() if code == 0 else ""

    def dirty_paths(self) -> list[str]:
        code, out, _ = self.run("status", "--porcelain")
        if code != 0:
            return []
        return [line[3:].strip() for line in out.splitlines() if line.strip()]

    def resolve(self, ref: str) -> str:
        """The exact 40-hex SHA `ref` names locally, or "" when it names nothing."""

        clean = str(ref or "").strip()
        if not clean:
            return ""
        code, out, _ = self.run("rev-parse", "--verify", "--quiet", f"{clean}^{{commit}}")
        return out.strip() if code == 0 else ""

    def merge_base(self, a: str, b: str) -> str:
        code, out, _ = self.run("merge-base", str(a), str(b))
        return out.strip() if code == 0 else ""

    def conflicted_paths(self) -> tuple[str, ...]:
        code, out, _ = self.run("diff", "--name-only", "--diff-filter=U")
        if code != 0:
            return ()
        return tuple(line.strip() for line in out.splitlines() if line.strip())

    def in_progress(self) -> str:
        """Which multi-step operation, if any, the repository is currently mid-way through."""

        git_dir = self.root / ".git"
        for marker, label in (
            ("MERGE_HEAD", "merge"),
            ("CHERRY_PICK_HEAD", "cherry_pick"),
            ("REVERT_HEAD", "revert"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
        ):
            if (git_dir / marker).exists():
                return label
        return ""

    # -- operations ---------------------------------------------------------------------

    def _result(self, operation: str, argv: tuple[str, ...], code: int, out: str, err: str) -> GitResult:
        conflicted = self.conflicted_paths()
        if conflicted:
            return GitResult(
                operation=operation,
                ok=False,
                status="conflict",
                argv=argv,
                returncode=code,
                stdout=out,
                stderr=err,
                head=self.head(),
                conflicted_paths=conflicted,
                detail=(
                    "the operation stopped on a conflict and the tree is left in the conflicted "
                    "state; resolve the named paths or run the `restore` operation to abort"
                ),
            )
        if code != 0:
            return GitResult(
                operation=operation,
                ok=False,
                status="failed",
                argv=argv,
                returncode=code,
                stdout=out,
                stderr=err,
                head=self.head(),
            )
        return GitResult(
            operation=operation, ok=True, status="executed", argv=argv, returncode=code, stdout=out, stderr=err, head=self.head()
        )

    def create_branch(self, name: str, *, start_point: str = "") -> GitResult:
        clean = str(name or "").strip()
        if not clean:
            return GitResult("branch", False, "invalid_arguments", (), detail="a branch needs a name")
        ruled = self._rules_refusal("branch", names=(clean,))
        if ruled is not None:
            return ruled
        argv = ("switch", "-c", clean) + ((str(start_point).strip(),) if str(start_point).strip() else ())
        code, out, err = self.run(*argv)
        return self._result("branch", argv, code, out, err)

    def commit(self, *, message: str, paths: tuple[str, ...] = ()) -> GitResult:
        """Stage and commit. The vertical cannot reach a push without one, so it is typed here
        rather than left to a shell command that no contract describes."""

        text = str(message or "").strip()
        if not text:
            return GitResult("commit", False, "invalid_arguments", (), detail="a commit needs a message")
        ruled = self._rules_refusal("commit", message=text, names=(self.branch(),))
        if ruled is not None:
            return ruled
        stage = ("add", "--", *paths) if paths else ("add", "-A")
        code, out, err = self.run(*stage)
        if code != 0:
            return self._result("commit", stage, code, out, err)
        # What is staged is exactly what the commit would carry; it is read only after staging.
        ruled = self._rules_refusal("commit", staged=True)
        if ruled is not None:
            return GitResult("commit", False, "refused_by_user_rule", stage, head=self.head(),
                             detail=ruled.detail + " The changes are staged, not committed.")
        argv = ("commit", "-m", text)
        code, out, err = self.run(*argv)
        if code != 0 and "nothing to commit" in f"{out}{err}".lower():
            return GitResult(
                "commit", False, "nothing_to_commit", argv, returncode=code, stdout=out, stderr=err,
                head=self.head(), detail="the working tree matches HEAD; there is nothing to commit",
            )
        return self._result("commit", argv, code, out, err)

    def cherry_pick(self, commit: str) -> GitResult:
        clean = str(commit or "").strip()
        if not clean:
            return GitResult("cherry_pick", False, "invalid_arguments", (), detail="a cherry-pick needs a commit")
        argv = ("cherry-pick", "--no-rerere-autoupdate", clean)
        code, out, err = self.run(*argv)
        return self._result("cherry_pick", argv, code, out, err)

    def revert(self, commit: str) -> GitResult:
        clean = str(commit or "").strip()
        if not clean:
            return GitResult("revert", False, "invalid_arguments", (), detail="a revert needs a commit")
        argv = ("revert", "--no-edit", clean)
        code, out, err = self.run(*argv)
        return self._result("revert", argv, code, out, err)

    def merge(self, onto: str, *, message: str = "") -> GitResult:
        clean = str(onto or "").strip()
        if not clean:
            return GitResult("merge", False, "invalid_arguments", (), detail="a merge needs something to merge")
        ruled = self._rules_refusal("merge", message=str(message or ""), rev_range=("HEAD", clean))
        if ruled is not None:
            return ruled
        argv = ("merge", "--no-ff", "--no-edit") + (("-m", str(message).strip()) if str(message).strip() else ()) + (clean,)
        code, out, err = self.run(*argv)
        return self._result("merge", argv, code, out, err)

    def tag(self, name: str, *, commit: str = "", message: str = "") -> GitResult:
        clean = str(name or "").strip()
        if not clean:
            return GitResult("tag", False, "invalid_arguments", (), detail="a tag needs a name")
        ruled = self._rules_refusal("tag", message=str(message or ""), names=(clean,))
        if ruled is not None:
            return ruled
        argv = ("tag", "-a", clean, "-m", str(message).strip() or clean) + (
            (str(commit).strip(),) if str(commit).strip() else ()
        )
        code, out, err = self.run(*argv)
        return self._result("tag", argv, code, out, err)

    def restore(self) -> GitResult:
        """Abort whatever multi-step operation is in progress. Refuses when none is."""

        pending = self.in_progress()
        if not pending:
            return GitResult(
                "restore", False, "nothing_in_progress", (), head=self.head(),
                detail="no merge, cherry-pick, revert or rebase is in progress; there is nothing to abort",
            )
        argv = {"merge": ("merge", "--abort"), "cherry_pick": ("cherry-pick", "--abort"),
                "revert": ("revert", "--abort"), "rebase": ("rebase", "--abort")}[pending]
        code, out, err = self.run(*argv)
        return self._result("restore", argv, code, out, err)

    def push_argv(self, *, remote: str, ref: str, sha: str) -> tuple[str, ...]:
        """The exact argv a push would run. Built, never executed, by this module.

        Note what it is NOT: there is no `--force`, no `+` refspec and no `--delete`. The
        default-denied operations have no expression here to reach.
        """

        # --no-recurse-submodules: push.recurseSubmodules would also push each submodule to its own
        # remote, a destination nothing here checked.
        return ("push", "--no-recurse-submodules", str(remote), f"{sha!s}:refs/heads/{ref!s}")


#: URL schemes git pushes over the network with its own transports (connect.c, remote-curl).
_NETWORK_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})


def _has_drive_prefix(text: str) -> bool:
    return os.name == "nt" and len(text) >= 2 and text[1] == ":" and text[0].isascii() and text[0].isalpha()


def _push_transport(url: str) -> str:
    """"network", "local" or "other", the way git's transport_get and url_is_local_not_ssh decide."""
    scheme_end = 0
    while scheme_end < len(url) and (url[scheme_end].isascii() and (url[scheme_end].isalnum() or url[scheme_end] in "+.-")):
        scheme_end += 1
    if url.startswith("::", scheme_end) and scheme_end:
        return "other"  # <helper>::<address> runs git-remote-<helper>
    if "://" in url:
        scheme = url.split("://", 1)[0]
        if scheme == "file":
            return "local"
        return "network" if scheme in _NETWORK_SCHEMES else "other"
    colon, slash = url.find(":"), url.find("/")
    if colon < 0 or (0 <= slash < colon) or _has_drive_prefix(url):
        return "local"
    return "network"  # scp-like [user@]host:path, which git runs over ssh


def _shown(url: str) -> str:
    """A URL safe to show: no user name or password."""
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@", r"\1", str(url))


def _destination_program_keys(git_dir: Path, *, timeout: float) -> list[str]:
    """Program-naming keys in a push destination's own config: every scope git reads there except the
    user's system and global files (so config.worktree too), plus any ``*hook`` key (receive-pack's
    ``gc --auto`` runs ``gc.recentObjectsHook``)."""
    probe = subprocess.run(
        ["git", "--git-dir", str(git_dir), "config", "--list", "--show-scope", "--includes", "--null"],
        capture_output=True, timeout=timeout, env=safe_git.safe_env(),
    )
    if probe.returncode not in (0, 1):  # 1: no entries at all
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    fields = [entry.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()
              for entry in probe.stdout.split(b"\0") if entry]
    if len(fields) % 2:
        raise RuntimeError("git config scopes could not be read")
    keys = set()
    for scope, key in zip(fields[0::2], fields[1::2], strict=True):
        if scope in {"system", "global"}:
            continue
        if safe_git.is_executable_key(key) or key.rsplit(".", 1)[-1].endswith("hook"):
            keys.add(key)
    return sorted(keys)


def _local_push_path(url: str) -> Path | None:
    """The folder a local push URL names, or None for a form VOOL does not read (git percent-decodes
    ``file://`` URLs, ignores their host part on POSIX and reads it as a UNC share on Windows)."""
    if not url.startswith("file://"):
        return Path(os.path.expanduser(url)) if url.startswith("~") else Path(url)
    rest = url[len("file://"):]
    if not rest.startswith("/") or "%" in rest or "[" in rest:
        return None
    if _has_drive_prefix(rest[1:]):
        rest = rest[1:]  # file:///C:/repo
    return Path(rest)


def _windows_destination_refusal(url: str, git_dir: Path, *, timeout: float) -> str:
    """On Windows, a push writes into a repository on this computer only when that repository's git
    folders are inside the folders marked in VOOL_WINDOWS_DISPOSABLE_WORKSPACES (owner decision,
    2026-10-20); a push to an online server is not affected. receive-pack writes objects and refs in
    the destination's git folder and, for a linked worktree, its shared git folder, so both are
    placed, and a link or hard link inside them, which would send those writes elsewhere, is refused
    too (core/git_write_redirects.py). Reproduced 2026-10-20: a push to a local bare repository
    outside the marked folders wrote its objects and branch there. Off Windows this returns ""."""
    from core import git_write_redirects, windows_write_gate

    if not windows_write_gate.active():
        return ""
    probe = subprocess.run(
        ["git", "--git-dir", str(git_dir), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=timeout, env=safe_git.safe_env(),
    )
    common = probe.stdout.strip()
    if probe.returncode != 0 or not common:
        return f"VOOL cannot tell where the push destination {url!r} keeps its git folder, so it does not push there"
    roots = windows_write_gate.flagged_roots()
    for place in dict.fromkeys((str(git_dir), common)):
        if not windows_write_gate._inside(place, roots):
            return (f"On Windows, VOOL pushes to a repository on this computer only inside the folders marked in "
                    f"{windows_write_gate.ENV_VAR}; the push destination {url!r} keeps its git folder at {place}, "
                    "which is outside them")
    try:
        found = git_write_redirects.refusal_for([("the push destination", git_dir, str(git_dir), common)],
                                                timeout=timeout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return f"the push destination {url!r} could not be checked ({type(exc).__name__})"
    return f"the push destination {url!r}: {found}" if found else ""


def _windows_push_source_refusal(root: Path, *, timeout: float) -> str:
    """On Windows, why a push from the repository at `root` could write outside the marked folders
    ("" when it cannot): every push updates this repository's own refs (where the remote's branch now
    is), so a link or hard link in its git folders, or a key its config sets that moves writes, is
    refused as for repo.git (core/git_write_redirects.py), whatever the destination. Reproduced
    2026-10-20: with `.git/refs/remotes` linked to an outside folder, a push to a marked destination
    wrote the remote's branch there. Off Windows this returns ""."""
    from core import git_write_redirects, windows_write_gate

    if not windows_write_gate.active():
        return ""
    probe = subprocess.run(
        safe_git.hardened_argv(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"),
        capture_output=True, text=True, timeout=timeout, env=safe_git.safe_env(),
    )
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) != 2:
        return "VOOL cannot tell where this repository keeps its git folder, so it does not push from it"
    try:
        found = git_write_redirects.refusal_for([("the repository", Path(root), lines[0], lines[1])], timeout=timeout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return f"this repository could not be checked before the push ({type(exc).__name__})"
    return found


def _receiving_git_dir(path: Path, *, timeout: float) -> Path | None:
    """The repository ``git receive-pack <path>`` enters: the first of path/.git, path, path.git/.git
    and path.git that is a git directory (setup.c enter_repo), or None."""
    for candidate in (path / ".git", path, Path(f"{path}.git") / ".git", Path(f"{path}.git")):
        if not candidate.exists():
            continue
        probe = subprocess.run(
            ["git", "--git-dir", str(candidate), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, timeout=timeout, env=safe_git.safe_env(),
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return Path(probe.stdout.strip())
    return None


__all__ = ["DEFAULT_DENIED", "GIT_TIMEOUT_SECONDS", "OPERATIONS", "GitResult", "GitRunner"]
