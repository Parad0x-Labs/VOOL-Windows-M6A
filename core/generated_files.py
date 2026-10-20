"""List the files VOOL has generated, across all chats, for the console's Files panel.

The panel is the answer to "where did that render / doc go?": one place to scroll (newest first),
sort, and search everything VOOL produced. This reads REAL on-disk locations — it never invents
entries. Files can be revealed in Finder (File Explorer on Windows) via the owner-local endpoint, and
only files that live under a known root can be opened (so the endpoint can't be used to reach arbitrary
paths). Add extra roots with VOOL_FILES_DIRS (os.pathsep-separated).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from core.runtime_paths import active_vool_home, runtime_auxiliary_path

# Sidecar index: which chat (session) + prompt produced each generated file, so the Files panel can
# group/sort "by chat". Written at save time; a file with no index entry simply has no chat attached.
_INDEX_LOCK = threading.Lock()


def _index_path() -> Path:
    return active_vool_home() / "data" / "generated_files_index.json"


def _load_index() -> dict[str, dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def record_generated_file(path: str, *, session_id: str = "", prompt: str = "") -> None:
    """Tag a just-saved generated file with the chat + prompt that made it (best-effort, fail-soft)."""
    real = os.path.realpath(os.path.expanduser(str(path or "")))
    if not real:
        return
    entry = {"session_id": str(session_id or "").strip(), "prompt": str(prompt or "").strip()[:200]}
    with _INDEX_LOCK:
        index = _load_index()
        index[real] = entry
        # Keep the index from growing without bound.
        if len(index) > 5000:
            index = dict(list(index.items())[-4000:])
        out = _index_path()
        with contextlib.suppress(OSError):
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".tmp")
            tmp.write_text(json.dumps(index), encoding="utf-8")
            os.replace(tmp, out)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".heic"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".gif"}
_DOC_EXT = {".md", ".txt", ".pdf", ".json", ".csv", ".html", ".srt"}
_MAX_FILES = 1000


def _default_roots() -> list[Path]:
    """Where VOOL writes generated media/docs. Renders land in ~/Pictures/VOOL-Renders today."""
    roots = [runtime_auxiliary_path("generated", legacy=Path.home() / "Pictures" / "VOOL-Renders")]
    extra = str(os.environ.get("VOOL_FILES_DIRS") or "").strip()
    if extra:
        for part in extra.split(os.pathsep):
            cleaned = part.strip()
            if cleaned:
                roots.append(Path(os.path.expanduser(cleaned)))
    # De-dupe by realpath while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = os.path.realpath(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def files_roots() -> list[str]:
    return [str(r) for r in _default_roots()]


def _file_type(suffix: str) -> str:
    s = str(suffix or "").lower()
    if s in _IMAGE_EXT:
        return "image"
    if s in _VIDEO_EXT:
        return "video"
    if s in _DOC_EXT:
        return "doc"
    return "file"


def list_generated_files(*, limit: int = 500) -> dict[str, Any]:
    """Every generated file under the known roots, newest first, tagged with the chat/prompt that made
    it (when known). {files, count, shown, roots}."""
    index = _load_index()
    items: list[dict[str, Any]] = []
    for root in _default_roots():
        if not root.is_dir():
            continue
        for entry in root.rglob("*"):
            if len(items) >= _MAX_FILES:
                break
            try:
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                st = entry.stat()
            except OSError:
                continue
            tag = index.get(os.path.realpath(str(entry))) or {}
            items.append({
                "name": entry.name,
                "path": str(entry),
                "type": _file_type(entry.suffix),
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),   # epoch seconds; the UI formats it
                "root": str(root),
                "session_id": str(tag.get("session_id") or ""),
                "prompt": str(tag.get("prompt") or ""),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    shown = items[: max(1, int(limit))]
    return {"files": shown, "count": len(items), "shown": len(shown), "roots": files_roots()}


def path_is_allowed(path: str) -> bool:
    """True only when ``path`` resolves to a file under one of the known roots (never an arbitrary path)."""
    raw = str(path or "").strip()
    if not raw:
        return False
    try:
        target = os.path.realpath(os.path.expanduser(raw))
    except OSError:
        return False
    return _resolved_is_allowed(target)


def _resolved_is_allowed(target: str) -> bool:
    """True when the already resolved ``target`` is one of the known roots or lies under one."""
    for root in _default_roots():
        r = os.path.realpath(str(root))
        if target == r or target.startswith(r + os.sep):
            return True
    return False


def host_is_windows() -> bool:
    """The one check for "this host is Windows" behind Show in folder (the page and both routes ask it).

    Windows has no Finder, no Preview and no ``open`` command; every other host keeps ``open``."""
    return sys.platform == "win32"


def _explorer_argv(*args: str) -> list[str] | None:
    """File Explorer's argument list ending in ``args``, or None when it cannot be built safely.

    Windows' own explorer.exe by absolute path: a bare name would be looked up in the app's folder and
    the current folder before the Windows folder. Explorer splits its command line on commas, and that
    it keeps a quoted path with a comma whole is not proven, so any path with a comma is refused rather
    than guessed at. Explorer is started in the Windows folder (``cwd``), never in VOOL's own folder.
    """
    if "," in args[-1]:
        return None
    windir = str(os.environ.get("SystemRoot") or os.environ.get("windir") or "")
    exe = os.path.join(windir, "explorer.exe")
    if not os.path.isabs(windir) or not os.path.isfile(exe):
        return None
    return [exe, *args]


def open_folder_in_explorer(folder: str) -> bool:
    """Windows: show a project's folder in File Explorer, selected in the folder that holds it (an
    argument list, never a shell).

    ``folder`` is a registered project root, stored symlink-resolved; it must still resolve to itself
    and be a folder, so a link or junction swapped in since is refused. ``/select,`` only selects:
    Explorer handed a plain path would run a file with its default app, and the folder could be swapped
    for a file between this check and Explorer reading its command line."""
    real = os.path.realpath(folder)
    if os.path.normcase(real) != os.path.normcase(folder) or not os.path.isdir(real):
        return False
    args = _explorer_argv("/select,", real)
    if args is None:
        return False
    import subprocess

    try:
        subprocess.Popen(args, cwd=os.path.dirname(args[0]))
        return True
    except Exception:
        return False


def open_file(path: str, *, reveal: bool = True) -> bool:
    """Open a generated file. ``reveal`` shows it in Finder (``open -R``); otherwise it opens in the
    default app (``open``, e.g. Preview for an image). Refuses anything outside the known roots.

    On Windows only ``reveal`` exists: File Explorer opens the file's folder with the file selected
    (``explorer.exe /select, <path>``). Opening is refused there, because Windows' default action for
    a .bat/.exe/.lnk/.ps1/.js artifact is to run it."""
    if not path_is_allowed(path):
        return False
    import subprocess

    real = os.path.realpath(os.path.expanduser(path))
    options: dict[str, Any] = {}
    if host_is_windows():
        # Explorer gets exactly the resolved path checked against the roots here.
        # "/select," and the path are separate arguments, so a path with spaces is quoted on its own:
        # explorer.exe /select, "C:\...\my render.png". Quoted together, Explorer ignores the switch.
        shown = reveal and _resolved_is_allowed(real) and os.path.exists(real)
        args = _explorer_argv("/select,", real) if shown else None
        if args is None:
            return False
        options["cwd"] = os.path.dirname(args[0])
    else:
        args = ["open", "-R", real] if reveal else ["open", real]
    try:
        subprocess.Popen(args, **options)
        return True
    except Exception:
        return False


_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tiff": "image/tiff", ".heic": "image/heic",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/x-m4v",
    ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
    ".json": "application/json", ".csv": "text/csv", ".html": "text/html; charset=utf-8",
}
_MAX_RAW_BYTES = 64 * 1024 * 1024


def content_type_for(path: str) -> str:
    return _MIME.get(os.path.splitext(str(path or ""))[1].lower(), "application/octet-stream")


def read_file_bytes(path: str) -> bytes | None:
    """Bytes of a generated file for inline display, or None. Confined to the known roots + size-capped."""
    if not path_is_allowed(path):
        return None
    real = os.path.realpath(os.path.expanduser(str(path)))
    try:
        if os.path.getsize(real) > _MAX_RAW_BYTES:
            return None
        with open(real, "rb") as handle:
            return handle.read()
    except OSError:
        return None


__all__ = [
    "content_type_for",
    "files_roots",
    "host_is_windows",
    "list_generated_files",
    "open_file",
    "open_folder_in_explorer",
    "path_is_allowed",
    "read_file_bytes",
]
