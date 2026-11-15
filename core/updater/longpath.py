r"""Extended-length (``\\?\``) spellings for the updater's snapshot and restore file calls on Windows.

With LongPathsEnabled=0, a Win32 file call refuses a path past MAX_PATH (260 characters, about 248
for a folder) with WinError 3. The runtime snapshot mirrors the VOOL home's layout under
``data\update_v2\transactions\<txid>\runtime-snapshot``, so a deep home overflows there (CARRY item 1).
``io_path`` gives the extended-length spelling of a full local drive path, which has no such limit.

It is applied only to the argument of an OS call. Containment checks, the snapshot index, journals and
status keep the plain spelling: ``core.windows_path_policy`` calls every ``\\?\`` value a device path.
The prefix also switches off Win32 normalisation (``..``, ``/``, trailing dots and spaces, CON and NUL),
so only a full local drive path is accepted, it is normalised first, and a reserved device name or a
name ending in a dot or space is refused rather than created. Anything else (a share, a device path,
a root- or drive-relative or relative path) raises ValueError: unknown forms fail closed. Off Windows
the path is returned unchanged.
"""
from __future__ import annotations

import ntpath
import os
import re

from core import windows_path_policy

#: Patched by tests to drive the Windows spelling on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"
PREFIX = "\\\\?\\"
_RESERVED_RE = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3]", re.IGNORECASE)


def _component_problem(name: str) -> str:
    if name in (".", ".."):
        return "a relative component"
    if name != name.rstrip(" ."):
        return "a name ending in a dot or space"
    if _RESERVED_RE.fullmatch(name.split(".", 1)[0].rstrip(" ")):
        return "a reserved device name"
    return ""


def _full_drive_path(spelled: str, original: str) -> str:
    drive, rest = ntpath.splitdrive(spelled)
    if not (len(drive) == 2 and drive[1] == ":" and drive[0].isascii() and drive[0].isalpha()
            and rest.startswith("\\")):
        raise ValueError(f"not a full local drive path: {original!r}")
    for name in rest.split("\\"):
        problem = _component_problem(name) if name else ""
        if problem:
            raise ValueError(f"path holds {problem}: {original!r}")
    return spelled


def io_path(path: str | os.PathLike[str]) -> str:
    """The spelling to hand an OS call for `path`: extended-length on Windows, unchanged elsewhere."""
    text = os.fspath(path)
    if not _IS_WINDOWS:
        return text
    if not isinstance(text, str):
        raise ValueError("a bytes path has no extended-length spelling")
    spelled = text.replace("/", "\\")
    if spelled.startswith(PREFIX):
        return PREFIX + _full_drive_path(spelled[len(PREFIX):], text)
    if windows_path_policy.classify(spelled):
        raise ValueError(f"not a full local drive path: {text!r}")
    return PREFIX + _full_drive_path(ntpath.normpath(spelled), text)


__all__ = ["PREFIX", "io_path"]
