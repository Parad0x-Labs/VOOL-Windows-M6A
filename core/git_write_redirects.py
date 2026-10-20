"""Where a repository can send git's writes beyond the folders git reports for it.

The Windows write gate places a git operation by the top of its work tree, its git folder and its
shared git folder, as git reports them (core/windows_write_gate.py). From inside those folders a
repository can still send git's writes elsewhere:
- a link in its git folders (a symbolic link, a junction or another link `core.fs_links` counts).
  git follows one in place of a folder it writes in (objects, refs, refs/heads, refs/tags, logs, rr-cache, lfs,
  modules) and in place of a file it writes in place or takes a lock on (index, COMMIT_EDITMSG,
  MERGE_MSG, MERGE_MODE, AUTO_MERGE), so the write lands where the link points;
- a hard link. A file git rewrites in place (COMMIT_EDITMSG, MERGE_MSG, MERGE_MODE, AUTO_MERGE) that
  is also another file's name changes that other file. Under `objects/` git never rewrites a file:
  it writes a new one and renames it into place, and `git clone` of a local folder hard-links its
  objects, so a hard link there is allowed (git may still touch such an object's time stamp);
- Git LFS, which the system config runs as a filter (Git for Windows installs it so), keeps its
  objects and temp files in `lfs.storage` when the repository's own config sets it, and other
  `lfs.*` keys it reads from that config choose transfer programs. Every repository-set `lfs.*` key
  is refused except the few listed in `_LFS_KEYS_ALLOWED`;
- automatic housekeeping. A commit runs `git maintenance run --auto`, and when the repository has
  enough packs its gc repacks everything; a repository-set `gc.repackFilterTo` then writes the
  objects `gc.repackFilter` selects into a pack in that folder, wherever it is (`_KEYS_REFUSED`).
  `--expire-to`, git's other repack destination, is a command-line option only, which VOOL never
  passes (checked against git's own source, 2.56 development, 2026-10-20).
Reproduced 2026-10-20 on Linux (git 2.43.0, git-lfs 3.4.1) through VOOL's GitRunner, with the
repository's folders inside a marked folder and the gate's checks passing: commits, branches,
tags, merges and a conflicting cherry-pick wrote outside through each of the above.
A link, a hard link or a config key the gate cannot read is refused too (fail closed).
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
#: `lfs.<url>.locksverify`) and the server and fetch choices a user sets there.
_LFS_KEYS_ALLOWED = re.compile(
    r"lfs\.(repositoryformatversion|url|pushurl|locksverify|fetchinclude|fetchexclude"
    r"|[a-z][a-z0-9+.-]*://.*\.(access|locksverify))"
)
#: Repository-set keys that name a folder outside the repository for git's writes.
_KEYS_REFUSED = frozenset({"gc.repackfilterto"})
#: Config scopes that belong to the user, not to the repository (``git config --show-scope``).
_USER_CONFIG_SCOPES = frozenset({"system", "global", "command"})


def refusal(git_dir: str, common_dir: str, cwd: Path, *, timeout: float = 30) -> str:
    """Why git's writes in this repository cannot be confined to the folders git reports ("" when
    they can): a link or hard link in its git folders, or a key its own config sets that moves writes
    (an `lfs.*` key, `gc.repackFilterTo`)."""
    found = link_refusal(git_dir, common_dir)
    if found:
        return found
    keys = _redirecting_keys(cwd, timeout=timeout)
    if keys:
        return ("the repository's own git config sets keys that can move git's writes elsewhere: "
                + ", ".join(keys))
    return ""


def link_refusal(*folders: str) -> str:
    """A sentence naming the first link or hard link under any of the git `folders`, or "". A push
    to a local repository asks this of the receiving repository's git folders (core/repoops/gitops.py)."""
    for folder in dict.fromkeys(folders):
        found = _link_in(Path(folder))
        if found:
            return found
    return ""


def _link_in(root: Path) -> str:
    """A sentence naming the first link or hard link under the git folder `root`, or ""."""
    pending = [(root, "")]
    while pending:
        folder, relative = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as exc:
            return f"the repository's git folder could not be checked for links ({type(exc).__name__})"
        for entry in entries:
            name = f"{relative}/{entry.name}" if relative else entry.name
            try:
                status = os.lstat(entry.path)
            except OSError as exc:
                return f"the repository's git folder could not be checked for links ({type(exc).__name__})"
            if fs_links.is_link_stat(status):
                return f"the repository's git folder holds a link ({name}) that can send git's writes elsewhere"
            if stat.S_ISDIR(status.st_mode):
                pending.append((Path(entry.path), name))
            elif status.st_nlink > 1 and not (name == "objects" or name.startswith("objects/")):
                return (f"a file in the repository's git folder ({name}) is also another file's name (a hard link), "
                        "so git's writes to it can change a file elsewhere")
    return ""


def _redirecting_keys(cwd: Path, *, timeout: float) -> list[str]:
    probe = subprocess.run(
        ["git", "-C", str(cwd), "config", "--show-scope", "--includes", "--null", "--list"],
        capture_output=True, timeout=timeout, env=safe_git.safe_env(),
    )
    if probe.returncode not in (0, 1):  # 1: no entries at all
        raise RuntimeError(f"git config could not be read ({probe.returncode})")
    fields = [entry.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower()
              for entry in probe.stdout.split(b"\0") if entry]
    if len(fields) % 2:
        raise RuntimeError("git config scopes could not be read")
    keys = {key for scope, key in zip(fields[::2], fields[1::2], strict=True)
            if scope not in _USER_CONFIG_SCOPES and (key in _KEYS_REFUSED or (
                key.startswith("lfs.") and not _LFS_KEYS_ALLOWED.fullmatch(key)))}
    return sorted(keys)


__all__ = ["link_refusal", "refusal"]
