"""Generic tools never write a repository's git metadata.

A repository's ``.git`` folder holds what git obeys: hooks it runs (``hooks/pre-commit`` on the
user's next own ``git commit``; Git for Windows runs a hook through its bundled sh whatever the
file's mode), config naming programs (``core.fsmonitor`` runs on the user's next ``git status``),
attributes choosing filters, and the config of every submodule under ``modules/``. A file written
there by a generic tool is a program git later runs for the user, outside any sandbox and with no
approval (reproduced 20 Oct 2026 through workspace.write_file, workspace.ensure_directory and
workspace.apply_unified_diff, on POSIX and through the Windows write gate). So a write is refused
when any part of its path names git metadata:

* a ``.git`` component, compared the way the filesystems that matter compare it: case-folded
  (macOS and Windows ignore case), with the trailing dots and spaces Windows drops, before an
  NTFS stream suffix (``.git::$INDEX_ALLOCATION``), and its 8.3 short name ``git~1`` (git's own
  ``is_ntfs_dotgit``). A ``.git`` FILE is covered too: it points git at a folder of its choosing;
* a folder git would itself open as a repository (``HEAD`` beside ``objects/`` and ``refs/``): a
  bare repository, or a git folder reached through a symlink or by its own path.

Paths are compared as written (made absolute) and with symlinks resolved, like
core.runtime_state_protection, which calls this for every write. Git's own operations
(repo.git, safe_git) do not go through here: git writes its metadata itself.

Not covered, stated: a write can still assemble a new bare-looking repository file by file
(``HEAD`` last); git opens one only when the user runs git inside it, which ``safe.bareRepository
= explicit`` (git 2.38+) refuses on the user's side.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

REFUSAL_PREFIX = "git_metadata"
_SHORT_NAME = re.compile(r"git~\d+")


def is_git_metadata_name(name: str) -> bool:
    """Whether a single path component names a ``.git`` entry on some filesystem git supports."""
    folded = name.split(":", 1)[0].rstrip(" .").casefold()
    return folded == ".git" or bool(_SHORT_NAME.fullmatch(folded))


def _is_git_directory(folder: Path) -> bool:
    # setup.c is_git_directory: HEAD, plus objects/ and refs/ (or a commondir file standing for them).
    try:
        if not (folder / "HEAD").is_file():
            return False
        if (folder / "commondir").is_file():
            return True
        return (folder / "objects").is_dir() and (folder / "refs").is_dir()
    except OSError:
        return True  # cannot rule it out: refuse


def _forms(path: Path) -> list[Path]:
    forms: list[Path] = []
    with contextlib.suppress(OSError, ValueError):
        forms.append(Path(os.path.abspath(path)))
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = path.resolve()
        if resolved not in forms:
            forms.append(resolved)
    return forms


def refusal(path: str | Path) -> str:
    """'' when a generic tool may write ``path``; otherwise the refusal to report (nothing was touched)."""
    candidate = Path(str(path)).expanduser()
    for form in _forms(candidate):
        if any(is_git_metadata_name(part) for part in form.parts):
            break
        if any(_is_git_directory(folder) for folder in (form, *form.parents)):
            break
    else:
        return ""
    return (f"{REFUSAL_PREFIX}: `{candidate}` is inside a git repository's metadata (.git: its hooks, config and "
            "attributes are programs and settings git runs for you). Generic tools cannot change it; nothing was touched.")


__all__ = ["REFUSAL_PREFIX", "is_git_metadata_name", "refusal"]
