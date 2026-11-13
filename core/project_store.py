"""Server-side project registry: a project is a named local folder a chat can be bound to.

Codex-style isolation for VOOL. A chat bound to a project operates in that project's folder and (with
per-project context scoping) never sees another project's files or memory. Projects live in
``<data>/projects.json``. Owner-local and path-validated: a root must be an existing absolute directory
on this machine. A project's id is derived from its real path, so re-adding the same folder is
idempotent (same id, name refreshed) rather than creating a duplicate.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import ntpath
import os
import re
import threading
from pathlib import Path
from typing import Any

from core import windows_path_policy
from core.memory.files import utcnow
from core.runtime_paths import data_path

_PROJECTS_FILE = "projects.json"
_PROJECTS_LOCK = threading.Lock()
_NAME_MAX = 80


def projects_path() -> Path:
    return data_path(_PROJECTS_FILE)


def _project_id_for(real_root: str) -> str:
    return "proj_" + hashlib.sha256(str(real_root).encode("utf-8")).hexdigest()[:12]


def _normalize_root(root: str) -> str:
    """Absolute, user-expanded, symlink-resolved path (so two spellings of one folder collapse to one id)."""
    raw = str(root or "").strip()
    if not raw:
        return ""
    return os.path.realpath(os.path.abspath(os.path.expanduser(raw)))


def _load() -> dict[str, dict[str, Any]]:
    path = projects_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in projects:
        if isinstance(item, dict) and item.get("id") and item.get("root"):
            out[str(item["id"])] = item
    return out


def _save(projects: dict[str, dict[str, Any]]) -> None:
    path = projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"projects": list(projects.values())}, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    with contextlib.suppress(OSError):  # last-good backup
        path.with_name(path.name + ".bak").write_text(payload, encoding="utf-8")


def list_projects() -> list[dict[str, Any]]:
    """All projects, newest first. Each: {id, name, root, created_at, exists}."""
    projects = list(_load().values())
    for entry in projects:
        root = str(entry.get("root") or "")
        # On Windows a stored share is listed as missing without being looked up.
        entry["exists"] = not windows_path_policy.refused_form(root, network_only=True) and os.path.isdir(root)
    projects.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return projects


def get_project(project_id: str) -> dict[str, Any] | None:
    return _load().get(str(project_id or "").strip())


def project_root(project_id: str) -> str | None:
    """The bound folder for a project, only if it still exists as a directory (else None)."""
    project = get_project(project_id)
    if not project:
        return None
    root = str(project.get("root") or "")
    if windows_path_policy.refused_form(root, network_only=True):
        return None  # a stored share on Windows: never looked up, so never bound or revealed
    return root if root and os.path.isdir(root) else None


# Folders that hold a whole computer's worth of files: binding VOOL to one of them is the same as
# binding it to the whole disk. Compared by real path, so a symlink spelling cannot slip past.
_SYSTEM_ROOTS = (
    "/Applications", "/Library", "/System", "/Users", "/Volumes", "/bin", "/boot", "/dev", "/etc",
    "/home", "/lib", "/opt", "/private", "/proc", "/sbin", "/sys", "/tmp", "/usr", "/var",
)


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


#: Windows known folders a project may be neither at nor inside (the OS, installed programs, machine-wide app
#: state), and the environment variables that name the same folders when the shell cannot answer.
_WINDOWS_SEALED_KNOWN = ("Windows", "ProgramFiles", "ProgramFilesX86", "ProgramData")
_WINDOWS_SEALED_ENV = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
#: Windows folders a project may sit inside but may not be: the profile, the folder holding every profile and
#: the per-user app-state folders (the Windows counterparts of ``~`` and ``~/Library``).
_WINDOWS_EXACT_KNOWN = ("LocalAppData", "RoamingAppData")


def _windows_norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path))).rstrip("\\")


def _windows_spellings(folders: list[str]) -> set[str]:
    """Each folder as spelled and, when it is on a drive of this machine, as it really resolves (a junction is
    judged by its target too). A folder on a share or a network drive is compared by its spelling only:
    resolving it would already be the SMB connection."""
    from core import windows_known_folders

    out: set[str] = set()
    for folder in folders:
        if not folder:
            continue
        out.add(_windows_norm(folder))
        if windows_known_folders.on_local_drive(folder):
            with contextlib.suppress(OSError, ValueError):
                out.add(_windows_norm(os.path.realpath(folder)))
    return out


def _windows_system_refusal(resolved: str) -> str:
    """Windows: a plain refusal when ``resolved`` is, or is inside, the Windows folder, a Program Files folder or
    ProgramData, or is the profile, the folder holding every profile or a per-user app-state folder; else ''.

    The folders come from the shell (SHGetKnownFolderPath, core/windows_known_folders.py), with the environment
    added. Both only add refusals, and when neither names the Windows folder the default on the system drive is
    refused, so a lookup that cannot answer never opens a system folder."""
    from core import windows_known_folders

    known = windows_known_folders.known_folder
    sealed = [known(name) or "" for name in _WINDOWS_SEALED_KNOWN]
    sealed += [os.environ.get(name, "") for name in _WINDOWS_SEALED_ENV]
    if not (known("Windows") or os.environ.get("SystemRoot") or os.environ.get("windir")):
        drive = ntpath.splitdrive(os.environ.get("SystemDrive", "") or "C:")[0] or "C:"
        sealed += [drive + "\\Windows", drive + "\\Program Files", drive + "\\Program Files (x86)",
                   drive + "\\ProgramData"]
    profiles = [known("Profile") or "", os.environ.get("USERPROFILE", ""), os.path.expanduser("~")]
    profiles = [p for p in profiles if p and p != "~"]
    exact = [p for p in profiles if p] + [ntpath.dirname(ntpath.normpath(p)) for p in profiles if p]
    exact += [ntpath.join(p, "AppData") for p in profiles if p]
    exact += [known(name) or "" for name in _WINDOWS_EXACT_KNOWN]
    target = _windows_norm(resolved)
    for folder in _windows_spellings(sealed):
        if folder and (target == folder or target.startswith(folder + "\\")):
            return "that is a system folder. Pick a normal folder, like Documents."
    for folder in _windows_spellings(exact):
        if folder and target == folder:
            if any(target == p for p in _windows_spellings(profiles)):
                return "that is your whole home folder. Pick one folder inside it, like Documents."
            if any(target == _windows_norm(ntpath.dirname(ntpath.normpath(p))) for p in profiles if p):
                return "that folder holds every user's files. Pick one folder inside your home folder, like Documents."
            return "that is a system folder. Pick a normal folder, like Documents."
    return ""


def _too_broad_root(resolved: str) -> str:
    """A plain refusal when ``resolved`` is the home folder, the folder holding every home, or a system folder; else ''."""
    if windows_path_policy.active():
        # Windows answers from the shell's known folders and the environment alone: the POSIX checks below
        # would resolve the home folder, which on a roaming profile is a share.
        return _windows_system_refusal(resolved)
    home = _real(os.path.expanduser("~"))
    if home and home not in {"~", os.sep} and resolved == home:
        return "that is your whole home folder. Pick one folder inside it, like Documents."
    if home and home not in {"~", os.sep} and resolved == _real(os.path.dirname(home)):
        return "that folder holds every user's files. Pick one folder inside your home folder, like Documents."
    if home and resolved == _real(os.path.join(home, "Library")):
        return "that is a system folder. Pick a normal folder, like Documents."
    if any(resolved == _real(p) for p in _SYSTEM_ROOTS):
        return "that is a system folder. Pick a normal folder, like Documents."
    return ""


def _network_drive(raw: str) -> str:
    """On Windows, the drive letter (`Z:`) of `raw` when Windows does not call that drive local; else ""."""
    from core import windows_known_folders

    if not windows_known_folders._IS_WINDOWS:
        return ""
    drive = ntpath.splitdrive(os.path.expanduser(raw))[0]
    if not re.fullmatch(r"[A-Za-z]:", drive):
        return ""
    try:
        local = windows_known_folders._drive_is_remote(drive.upper()) is False
    except Exception:  # an unknown drive type counts as remote (fail closed)
        local = False
    return "" if local else drive.upper()


def create_project(name: str, root: str) -> tuple[bool, Any]:
    """Register a folder as a project. Returns (True, project) or (False, error_message).

    Idempotent by real path: adding the same folder twice returns the existing project (name refreshed).
    The root must be an existing absolute directory (or start with ``~``). A relative path is refused
    instead of being resolved against the server's own working directory (which would bind VOOL to its
    install folder), and so are the filesystem root, the whole home folder, the folder that holds every user's home, and the
    operating system's own folders. On Windows a share or a partial path is refused by its spelling,
    before anything looks it up. Error messages are plain sentences that start in
    lower case (the callers prefix them) and name the resolved path where it helps.
    """
    raw = str(root or "").strip()
    if not raw:
        return False, "a project folder is required. Type its path or choose one."
    # Windows: a share or a partial path is refused by its spelling, before anything looks it up.
    non_local = windows_path_policy.refused_form(root)
    if non_local:
        return False, windows_path_policy.refusal_text(root, non_local)
    # Windows: a drive letter Windows does not call local (a mapped network drive is a share under a
    # letter, and a drive type Windows cannot name counts as one) is refused before realpath, which
    # would already open the SMB connection.
    network_drive = _network_drive(raw)
    if network_drive:
        return False, (
            f"the drive {network_drive} is a network drive (or one Windows can't identify), and VOOL only uses "
            "folders on this computer's own drives. Pick a folder on a local drive."
        )
    if not raw.startswith("~") and not os.path.isabs(raw):
        if os.name == "nt":
            return False, "type the whole folder path, starting with a drive letter or ~ (for example C:\\Users\\you\\Documents)."
        return False, "type the whole folder path, starting with / or ~ (for example ~/Documents)."
    resolved = _normalize_root(raw)
    # ``_normalize_root`` returns the drive root on Windows (``C:\\``), so comparing
    # with ``os.path.sep`` only protects POSIX. A filesystem root is its own parent on
    # every platform and must never become a project boundary.
    if Path(resolved).parent == Path(resolved):
        return False, "pick one folder, not the whole filesystem."
    refusal = _too_broad_root(resolved)
    if refusal:
        return False, refusal
    if not os.path.exists(resolved):
        return False, f"that folder does not exist: {resolved}. Check the spelling."
    if not os.path.isdir(resolved):
        return False, f"that is a file, and a project must be a folder: {resolved}."
    clean_name = " ".join(str(name or "").split()).strip()[:_NAME_MAX] or os.path.basename(resolved) or resolved
    pid = _project_id_for(resolved)
    with _PROJECTS_LOCK:
        projects = _load()
        existing = projects.get(pid) or {}
        entry = {
            "id": pid,
            "name": clean_name,
            "root": resolved,
            "emoji": existing.get("emoji") or "",   # a sidebar marker to tell projects apart at a glance
            "color": existing.get("color") or "",   # an optional accent colour (#rrggbb) tinting the sidebar + context bar
            "created_at": existing.get("created_at") or utcnow(),
        }
        projects[pid] = entry
        _save(projects)
    return True, {**entry, "exists": True}


_EMOJI_MAX = 8   # a couple of emoji code points (some emoji are multi-codepoint), never a long string
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")   # strict #rrggbb only — never arbitrary CSS


def normalize_color(color: str) -> str:
    """A stored colour is either '' (none) or a lowercase #rrggbb; anything else collapses to ''."""
    raw = str(color or "").strip().lower()
    return raw if _HEX_COLOR.match(raw) else ""


def set_project_emoji(project_id: str, emoji: str) -> bool:
    """Set (or clear with "") a project's sidebar emoji. Returns True if a project was updated."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    clean = "".join(str(emoji or "").split())[:_EMOJI_MAX]   # strip whitespace, cap length
    with _PROJECTS_LOCK:
        projects = _load()
        entry = projects.get(pid)
        if not entry:
            return False
        entry["emoji"] = clean
        projects[pid] = entry
        _save(projects)
    return True


def set_project_color(project_id: str, color: str) -> bool:
    """Set (or clear with "") a project's accent colour (#rrggbb). Returns True if a project was updated."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    clean = normalize_color(color)
    with _PROJECTS_LOCK:
        projects = _load()
        entry = projects.get(pid)
        if not entry:
            return False
        entry["color"] = clean
        projects[pid] = entry
        _save(projects)
    return True


_APPROVAL_KEY = "low_risk_approval"


def get_project_low_risk_approval(project_id: str) -> dict[str, Any]:
    """The standing 'allow low-risk actions in this project' grant, or {} when there is none.

    Low-risk means reads, searches, and file writes that land INSIDE this project's folder. The grant
    is deliberately NOT a permission set: it can only turn an approval PROMPT into an allow for that
    bounded class. Deletes, moves, network sends, spend, and anything resolving outside the root are
    not covered and keep prompting. See ``core.mode_permission_policy`` for the enforcement.
    """
    pid = str(project_id or "").strip()
    if not pid:
        return {}
    entry = _load().get(pid) or {}
    grant = entry.get(_APPROVAL_KEY)
    return dict(grant) if isinstance(grant, dict) and grant.get("granted_at") else {}


def set_project_low_risk_approval(
    project_id: str,
    *,
    granted_at: str = "",
    granted_by_session: str = "",
    actions: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Persist the standing low-risk grant on the project entry. Returns the stored grant, or {}."""
    pid = str(project_id or "").strip()
    if not pid:
        return {}
    grant = {
        "granted_at": str(granted_at or utcnow()),
        "granted_by_session": str(granted_by_session or "")[:120],
        "actions": [str(item)[:60] for item in list(actions)[:24]],
    }
    with _PROJECTS_LOCK:
        projects = _load()
        entry = projects.get(pid)
        if not entry:
            return {}
        entry[_APPROVAL_KEY] = grant
        projects[pid] = entry
        _save(projects)
    return dict(grant)


def clear_project_low_risk_approval(project_id: str) -> bool:
    """Revoke the standing low-risk grant. Returns True if one was removed."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    with _PROJECTS_LOCK:
        projects = _load()
        entry = projects.get(pid)
        if not entry or not isinstance(entry.get(_APPROVAL_KEY), dict):
            return False
        del entry[_APPROVAL_KEY]
        projects[pid] = entry
        _save(projects)
    return True


def delete_project(project_id: str) -> bool:
    """Forget a project (does not touch the folder or its files). Returns True if one was removed."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    with _PROJECTS_LOCK:
        projects = _load()
        if pid not in projects:
            return False
        del projects[pid]
        _save(projects)
    return True


__all__ = [
    "clear_project_low_risk_approval",
    "create_project",
    "delete_project",
    "get_project",
    "get_project_low_risk_approval",
    "list_projects",
    "normalize_color",
    "project_root",
    "projects_path",
    "set_project_color",
    "set_project_emoji",
    "set_project_low_risk_approval",
]
