"""Where a repository can send git's writes beyond the folders git reports for it.

The Windows write gate places a git operation by the top of its work tree, its git folder and its
shared git folder, as git reports them (core/windows_write_gate.py). From inside those folders a
repository can still send git's writes elsewhere:
- a link in its git folders (a symbolic link, a junction or another link `core.fs_links` counts).
  git follows one in place of a folder it writes in (objects, refs, refs/heads, refs/tags, logs, rr-cache, lfs,
  modules) and in place of a file it writes in place or takes a lock on (index, COMMIT_EDITMSG,
  MERGE_MSG, MERGE_MODE, AUTO_MERGE), so the write lands where the link points;
- a hard link. A file git rewrites in place (COMMIT_EDITMSG, MERGE_MSG, MERGE_MODE, AUTO_MERGE) that
  is also another file's name changes that other file. In an object store (`objects/`, a submodule's
  `modules/<name>/objects/`, Git LFS's `lfs/objects/`) git and git-lfs never rewrite a file: they write
  a new one and rename it into place, as they do for `objects/info/` (`commit-graph`, `packs`,
  `alternates`) and a pack's indexes, bitmaps and `multi-pack-index`, and a clone, `git submodule add`
  or `git lfs pull` from a local folder hard-links those files, so a hard link there is allowed (git
  may still touch such a file's time stamp; the files git does write in place there are listed at
  `_OBJECT_STORE_FILE`);
- Git LFS, which the system config runs as a filter (Git for Windows installs it so), keeps its
  objects and temp files in `lfs.storage` when the repository's own config sets it, and other
  `lfs.*` keys it reads from that config choose transfer programs. Every repository-set `lfs.*` key
  is refused except the ones listed in `_LFS_KEYS_ALLOWED`;
- automatic housekeeping. A commit runs `git maintenance run --auto`, and a push into a local
  repository runs that repository's auto gc; when the repository has enough packs its gc repacks
  everything, and a repository-set `gc.repackFilterTo` then writes the objects `gc.repackFilter`
  selects into a pack in that folder, wherever it is. `--expire-to`, git's other repack destination,
  is a command-line option only, which VOOL never passes (checked against git's own source, 2.56
  development, 2026-10-20);
- a push over http(s) saves its cookies to a repository-set `http.cookieFile` when
  `http.saveCookies` is on, wherever that file is (refused by its key, as `_refused_key` reads it);
- a checked-out submodule. git runs inside it (status, diff and commit in the superproject), and
  git-lfs with it, under the submodule's own config and in the submodule's own git folders, so each
  is checked like the superproject (`repositories`).
Reproduced 2026-10-20 on Linux (git 2.43.0, git-lfs 3.4.1) through VOOL's GitRunner, with the
repository's folders inside a marked folder and the gate's checks passing: commits, branches,
tags, merges, a conflicting cherry-pick and a push wrote outside through each of the above, apart
from the cookie file, which is refused on git's documentation alone.
A link, a hard link or a config key the gate cannot read is refused too (fail closed); an entry
another git process removes while the folder is walked is skipped, since a link put in its place
would be seen. On Windows a file that cannot be opened (another program holds it with no sharing)
reports one name, so a hard link to it is not seen; and a folder can change after the walk, as for
every check the gate makes by path.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from core import fs_links, safe_git

#: Repository-set `lfs.*` keys that move no write and name no program: what git-lfs itself writes
#: into a repository's config (`lfs.repositoryformatversion`, `lfs.<url>.access`,
#: `lfs.<url>.locksverify`) and the server, fetch and transfer-tuning choices a user sets there.
_LFS_KEYS_ALLOWED = re.compile(
    r"lfs\.(repositoryformatversion|url|pushurl|locksverify|fetchinclude|fetchexclude|fetchrecentalways"
    r"|concurrenttransfers|allowincompletepush|setlockablereadonly|skipdownloaderrors|activitytimeout"
    r"|dialtimeout|tlstimeout|keepalive|basictransfersonly|tustransfers"
    r"|transfer\.(maxretries|maxretrydelay|maxverifies|enablehrefrewrite)"
    r"|[a-z][a-z0-9+.-]*://.*\.(access|locksverify))"
)
#: Repository-set keys that name a place outside the repository for git's writes.
_KEYS_REFUSED = frozenset({"gc.repackfilterto", "http.cookiefile"})
#: Config scopes that belong to the user, not to the repository (``git config --show-scope``).
_USER_CONFIG_SCOPES = frozenset({"system", "global", "command"})
#: Files in an object store that git and git-lfs only ever create, rename into place or delete, the
#: only places a hard link is allowed (paths relative to a git folder). Checked against git's source
#: (2.56 development, 2026-10-20): a pack's `.promisor` is rewritten in place by a partial-clone fetch,
#: `objects/loose-object-idx` is appended to, and dumb-http `*.temp` downloads are resumed in place,
#: so a hard link at those is refused.
_HASH = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"  # SHA-1 or SHA-256
_OBJECT_STORE_FILE = re.compile(
    # A submodule's git folder, also inside a linked worktree's own git folder. No step of it is a
    # `logs` or `refs` folder: git appends to reflogs in place, and a branch may be named like an
    # object (`objects/ab/<38 hex>`), so `modules/x/logs/refs/heads/objects/...` is not an object store.
    r"(?:(?:worktrees/[^/]+/)?modules/(?:(?!(?:logs|refs)/)[^/]+/)+)?(?:"
    r"objects/[0-9a-f]{2}/[0-9a-f]{38}(?:[0-9a-f]{24})?"
    rf"|objects/pack/pack-{_HASH}\.(?:pack|idx|rev|bitmap|mtimes|keep)"
    rf"|objects/pack/multi-pack-index(?:-{_HASH}\.(?:bitmap|rev))?"
    rf"|objects/pack/multi-pack-index\.d/(?:multi-pack-index-chain|multi-pack-index-{_HASH}\.(?:midx|bitmap|rev))"
    r"|objects/info/(?:commit-graph|packs|alternates)"
    rf"|objects/info/commit-graphs/(?:commit-graph-chain|graph-{_HASH}\.graph)"
    r"|lfs/objects/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64})"
)
#: Nested submodules deeper than this are refused rather than checked (as core/safe_git.py does).
_MAX_SUBMODULE_DEPTH = 8


def repositories(cwd: Path, git_folders: tuple[str, str], *, timeout: float = 30) -> list[tuple[str, Path, str, str]]:
    """``(label, folder, git folder, shared git folder)`` of the repository git runs in at `cwd`, whose
    git folders the caller already asked git for, and of each checked-out submodule under it, nested
    ones included, as git reports their git folders. Outside a work tree git recurses into no
    submodule, so none is listed there."""
    found = [("the repository", Path(cwd), *git_folders)]
    if safe_git._rev_parse(Path(cwd), "--is-inside-work-tree", timeout=timeout) != "true":
        return found
    pending = [(Path(cwd), "", 0)]
    while pending:
        folder, prefix, depth = pending.pop()
        for path, submodule in safe_git._checked_out_submodules(folder, timeout=timeout):
            if depth >= _MAX_SUBMODULE_DEPTH:
                raise RuntimeError("submodules are nested too deeply to check")
            name = f"{prefix}{path}"
            found.append((f"submodule {name}", submodule, *_git_folders(submodule, timeout=timeout)))
            pending.append((submodule, f"{name}/", depth + 1))
    return found


def refusal(git_dir: str, common_dir: str, cwd: Path, *, timeout: float = 30) -> str:
    """Why git's writes in the one repository at `cwd` cannot be confined to the folders git reports
    ("" when they can): a link or hard link in its git folders, or a key its own config sets that
    moves writes (an `lfs.*` key, `gc.repackFilterTo`, `http.cookieFile`)."""
    return refusal_for([("the repository", Path(cwd), git_dir, common_dir)], timeout=timeout)


def refusal_for(repos: list[tuple[str, Path, str, str]], *, timeout: float = 30) -> str:
    """`refusal` for each of `repos` (as `repositories` lists them), each git folder walked once."""
    found = link_refusal(*(folder for _, _, git_dir, common_dir in repos for folder in (common_dir, git_dir)))
    if found:
        return found
    for label, _, git_dir, _ in repos:
        keys = _redirecting_keys(git_dir, timeout=timeout)
        if keys:
            whose = "the repository's own" if label == "the repository" else f"{label}'s own"
            return f"{whose} git config sets keys that can move git's writes elsewhere: " + ", ".join(keys)
    return ""


def link_refusal(*folders: str) -> str:
    """A sentence naming the first link or hard link under any of the git `folders`, or "". A folder
    inside another one listed is walked as part of it, not again."""
    for root in _outermost(folders):
        found = _link_in(root)
        if found:
            return found
    return ""


def _outermost(folders: tuple[str, ...]) -> list[Path]:
    """`folders` without repeats and without those inside another one (compared as they resolve)."""
    resolved = {}
    for folder in folders:
        resolved.setdefault(os.path.normcase(os.path.realpath(folder)), Path(folder))
    keys = sorted(resolved, key=len)
    kept: list[str] = []
    for key in keys:
        if not any(os.path.commonpath([key, outer]) == outer for outer in kept if _same_drive(key, outer)):
            kept.append(key)
    return [resolved[key] for key in kept]


def _same_drive(a: str, b: str) -> bool:
    return os.path.splitdrive(a)[0] == os.path.splitdrive(b)[0]


def _link_in(root: Path) -> str:
    """A sentence naming the first link or hard link under the git folder `root`, or ""."""
    pending = [(root, "")]
    while pending:
        folder, relative = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            if relative:  # removed by another git process since its folder was listed
                continue
            return "the repository's git folder could not be checked for links (FileNotFoundError)"
        except OSError as exc:
            return f"the repository's git folder could not be checked for links ({type(exc).__name__})"
        for entry in entries:
            name = f"{relative}/{entry.name}" if relative else entry.name
            try:
                # On Windows this comes from the folder listing, with the reparse tag, and opens nothing.
                status = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # removed since the folder was listed; a link in its place would be listed
            except OSError as exc:
                return f"the repository's git folder could not be checked for links ({type(exc).__name__})"
            if fs_links.is_link_stat(status):
                return f"the repository's git folder holds a link ({name}) that can send git's writes elsewhere"
            if stat.S_ISDIR(status.st_mode):
                pending.append((Path(entry.path), name))
                continue
            if _OBJECT_STORE_FILE.fullmatch(name):
                continue
            try:
                # The listing gives no link count on Windows, so the file itself is asked there.
                links = status.st_nlink if os.name != "nt" else os.lstat(entry.path).st_nlink
            except FileNotFoundError:
                continue
            except OSError as exc:
                return f"the repository's git folder could not be checked for links ({type(exc).__name__})"
            if links > 1:
                return (f"a file in the repository's git folder ({name}) is also another file's name (a hard link), "
                        "so git's writes to it can change a file elsewhere")
    return ""


def _git_folders(folder: Path, *, timeout: float) -> tuple[str, str]:
    probe = subprocess.run(
        safe_git.hardened_argv(folder, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"),
        capture_output=True, text=True, timeout=timeout, env=safe_git.safe_env(),
    )
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) != 2 or not all(os.path.isabs(line) for line in lines):
        raise RuntimeError(f"git cannot say where the git folders of {folder} are")
    return lines[0], lines[1]


def _refused_key(key: str) -> bool:
    if key in _KEYS_REFUSED or (key.startswith("http.") and key.endswith(".cookiefile")):
        return True
    return key.startswith("lfs.") and not _LFS_KEYS_ALLOWED.fullmatch(key)


def _redirecting_keys(git_dir: str, *, timeout: float) -> list[str]:
    """The keys the repository with git folder `git_dir` sets itself that move git's writes. The git
    folder is named, since `git -C` opens no repository when the user's safe settings
    (`safe.bareRepository`, ownership) say not to and then lists the user's keys only; a listing with
    no repository scope is refused rather than read as clean (every repository's config sets
    `core.repositoryformatversion`)."""
    probe = subprocess.run(
        ["git", "--git-dir", str(git_dir), "config", "--show-scope", "--includes", "--null", "--list"],
        capture_output=True, timeout=timeout, env=safe_git.safe_env(),
    )
    if probe.returncode != 0:
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    fields = [entry.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()
              for entry in probe.stdout.split(b"\0") if entry]
    if len(fields) % 2:
        raise RuntimeError("git config scopes could not be read")
    if "local" not in fields[::2]:
        raise RuntimeError("git listed none of the repository's own config")
    keys = {key for scope, key in zip(fields[::2], fields[1::2], strict=True)
            if scope not in _USER_CONFIG_SCOPES and _refused_key(key)}
    return sorted(keys)


__all__ = ["link_refusal", "refusal", "refusal_for", "repositories"]
