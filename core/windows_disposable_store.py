"""Windows: the owner's own list of disposable workspace folders, kept beside VOOL_WINDOWS_DISPOSABLE_WORKSPACES.

Clean-VM job 52 (port 76afbd36): an installed user could not make VOOL write any file, because the
only list of folders the Windows write gate (core/windows_write_gate.py) accepts was an environment
variable the developer sets before starting VOOL. Owner decision (Jesse, 2026-11-07): a folder can
also be marked from the VOOL window, with an OS confirmation. This module is that second source.

- One file, ``<VOOL home>/config/disposable_workspaces.json``. Only the native window bridge writes
  it (installer/bundle/vool_window.py ``mark_disposable_folder`` / ``unmark_disposable_folder``),
  after the OS folder picker and an OS confirm dialog that names the resolved folder; no HTTP route
  writes it. It is written owner-only (``ensure_private_dir``, the temp file protected before it is
  filled, then an atomic replace) and read only when ``verify_owner_only`` passes on the folder and the
  file. A file that is not owner-only, cannot be verified, cannot be parsed or has another schema adds
  NOTHING and is reported: unknown states fail closed, exactly as a corrupt file does.
- The file only ever ADDS candidates. Every entry still has to pass the gate's own ``_windows_root``
  (and the broker's checks when it runs), plus ``refusal_reason`` here, re-checked on every read.
- Adds take effect at the next start: the broker's list is fixed when it starts
  (docs/WINDOWS_BROKER.md section 4), so the gate takes the file's entries as they were at start
  (``start_folders``). An unmark shrinks at once: a start entry no longer in the live file is
  dropped, and a live file that cannot be read means none of the file's entries count.
- Residual, recorded in docs/WINDOWS_BROKER.md section 4: while confinement B is stopped, code VOOL
  runs as the user can write this file just as it can ``setx`` the variable. Code that can already
  write anywhere gains nothing by marking, and every mark/unmark through the bridge is audited and
  the broker logs the list it loaded, so a mark made any other way is visible.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import ntpath
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "vool.windows.disposable_workspaces.v1"
FILE_NAME = "disposable_workspaces.json"

#: Folders under the profile that hold credentials or app state but are not shell known folders.
_PROFILE_PRIVATE = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", "AppData")
#: Shell known folders a disposable folder may sit INSIDE but may not be, nor hold.
_KNOWN_EXACT = ("Profile", "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive")
#: Known folders nothing may be marked at, under, or above.
_KNOWN_SEALED = ("LocalAppData", "RoamingAppData", "ProgramData", "Windows",
                 "ProgramFiles", "ProgramFilesX86")

#: The file's entries as they were when this process first took them (the start list), per file.
_START: dict[str, list[str]] = {}
#: Patched by tests to drive the Windows reparse-point check on any platform.
_IS_NT = os.name == "nt"
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MarkRefused(ValueError):
    """A folder that may not be marked disposable; ``reason`` says why in plain words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def store_path(home: str | os.PathLike[str] | None = None) -> Path:
    if home is None:
        from core.runtime_paths import active_vool_home

        home = active_vool_home()
    return Path(home) / "config" / FILE_NAME


def entry_id(path: str) -> str:
    return hashlib.sha256(_norm(path).encode("utf-8")).hexdigest()[:16]


def _norm(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value)))


def normalised(value: str) -> str:
    """The spelling every comparison in this module uses (Windows rules: case and separators)."""
    return _norm(value)


def _at_or_under(folder: str, other: str) -> bool:
    f, o = _norm(folder), _norm(other).rstrip("\\")
    return f == o or f.startswith(o + "\\")


def _holds(folder: str, other: str) -> bool:
    """``folder`` is ``other`` or an ancestor of it."""
    return _at_or_under(other, folder)


def _known(name: str) -> str | None:
    from core import windows_known_folders

    return windows_known_folders.known_folder(name)


def _real(value: str) -> str:
    return os.path.realpath(value)


#: IO_REPARSE_TAG_CLOUD and CLOUD_1..CLOUD_F (OneDrive Files-On-Demand placeholders) share this
#: value once the 4-bit CLOUD_n field is masked out.
_CLOUD_TAG, _CLOUD_TAG_MASK = 0x9000001A, 0xFFFF0FFF
#: Patched by tests: the stat of one component, never followed.
_lstat = os.lstat


def _reparse_attribute(path: str) -> bool:
    """Whether ``path`` itself (not followed) is a reparse point that must be refused. Only a
    cloud-sync placeholder passes (its tag readable and a CLOUD tag); missing attributes, an
    unreadable tag, any other tag (mount points and symlinks are name surrogates) and an
    unreadable component are refused."""
    try:
        result = _lstat(path)
    except OSError:
        return True
    attributes = getattr(result, "st_file_attributes", None)
    if not isinstance(attributes, int):
        return True
    if not attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(result, "st_reparse_tag", None)
    if not isinstance(tag, int):
        return True
    return (tag & _CLOUD_TAG_MASK) != _CLOUD_TAG


def _link_on_the_way(path: str) -> str:
    """The first component of ``path`` that is, or may be, a link, junction or other reparse point,
    or ''. ``realpath`` would follow one silently, so anything redirecting on the way is refused
    rather than judged by its target. Fails closed: without ``os.path.isjunction`` (Python before
    3.12) no component can be cleared, and on Windows every component's own reparse attribute is
    read too, which also catches mount points and other reparse types."""
    isjunction = getattr(os.path, "isjunction", None)
    drive, rest = ntpath.splitdrive(path)
    current = drive + "\\" if drive else os.sep
    for part in [p for p in rest.replace("/", "\\").split("\\") if p]:
        current = os.path.join(current, part)
        if isjunction is None or os.path.islink(current) or isjunction(current):
            return current
        if _IS_NT and _reparse_attribute(current):
            return current
    return ""


def _with_real(folders: list[str]) -> list[str]:
    out: list[str] = []
    for folder in folders:
        out.append(folder)
        with contextlib.suppress(OSError, ValueError):
            out.append(_real(folder))
    return out


def appdata_family() -> list[str]:
    """LocalAppData, RoamingAppData and Profile\\AppData: the only sealed folders a temp subfolder
    may sit inside (the system temp folder normally lives in LocalAppData)."""
    family = [found for found in (_known("LocalAppData"), _known("RoamingAppData")) if found]
    profile = _known("Profile")
    if profile:
        family.append(ntpath.join(profile, "AppData"))
    return _with_real(family)


def protected_folders(extra: tuple[str, ...] = ()) -> tuple[list[str], list[str], list[str]]:
    """(sealed, exact, temp_roots): folders nothing may be marked at/under/above, folders that may
    only hold a marked folder, and the system temp root (``tempfile.gettempdir``, Windows'
    GetTempPath; a folder strictly inside it may lift the AppData family only)."""
    sealed: list[str] = []
    exact: list[str] = []
    profile = _known("Profile")
    for name in _KNOWN_SEALED:
        found = _known(name)
        if found:
            sealed.append(found)
    if profile:
        sealed.extend(ntpath.join(profile, name) for name in _PROFILE_PRIVATE)
    for name in _KNOWN_EXACT:
        found = _known(name)
        if found:
            exact.append(found)
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if system_root:
        sealed.append(system_root)
        drive = ntpath.splitdrive(system_root)[0]
        if drive:
            sealed.append(drive + "\\$Recycle.Bin")
    sealed.extend(str(p) for p in extra if p)
    temp_roots = _with_real([tempfile.gettempdir()])
    return _with_real(sealed), exact, temp_roots


def refusal_reason(path: str, *, extra_sealed: tuple[str, ...] = ()) -> str:
    """Why ``path`` may not be a disposable folder, or '' when it may. ``path`` is judged as the
    folder it really is (``realpath``), and a link or junction anywhere on the way is refused."""
    from core import windows_write_gate

    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        return "it is not a folder path"
    linked = _link_on_the_way(raw)
    if linked:
        return f"{linked} is a junction or link; mark the folder it points at instead"
    root = windows_write_gate._windows_root(raw)
    if root is None:
        return "it is a network, device, linked or missing folder"
    real = _real(raw)
    if ntpath.splitdrive(_norm(real))[1] in ("", "\\"):
        return "it is a whole drive"
    try:
        from core.runtime_paths import active_vool_home

        home = str(active_vool_home())
    except Exception:
        home = ""
    app_root = str(Path(__file__).resolve().parents[1])
    sealed, exact, temp_roots = protected_folders((home, app_root, *extra_sealed))
    for temp in temp_roots:
        if _holds(real, temp):
            return "it is or holds the temporary-files folder"
    # A folder strictly inside the temp root is allowed even though %TEMP% itself lives in
    # AppData -- but that lifts ONLY the AppData family, never VOOL home, the app, a caller's
    # sealed folder or a credential folder, wherever TEMP points.
    temp_of = [t for t in temp_roots if _at_or_under(real, t)]
    liftable = {_norm(f) for f in appdata_family()} if temp_of else set()
    for folder in sealed:
        if _holds(real, folder):
            return f"it is or holds {folder}"
        lifted = _norm(folder) in liftable and any(_holds(folder, t) for t in temp_of)
        if _at_or_under(real, folder) and not lifted:
            return f"it is inside {folder}"
    for folder in exact:
        if _holds(real, folder):
            return f"it is or holds {folder}; mark a folder inside it instead"
    return ""


def in_sync_root(path: str) -> bool:
    """Whether ``path`` sits in a OneDrive (or other cloud sync) folder: deletions there sync too."""
    roots = [os.environ.get(k, "") for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    found = _known("OneDrive")
    if found:
        roots.append(found)
    return any(r and _at_or_under(path, r) for r in roots) or "\\onedrive" in _norm(path)


def _read(path: Path) -> tuple[list[dict[str, str]], str]:
    """(entries, problem). A missing file is ([], ''). Every other doubt is ([], reason)."""
    from core.owner_only import verify_owner_only

    if not path.exists():
        return [], ""
    for target in (path.parent, path):
        report = verify_owner_only(target)
        if not report.ok:
            return [], f"not_owner_only:{report.reason}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"unreadable:{type(exc).__name__}"
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("folders"), list):
        return [], "unknown_schema"
    entries: list[dict[str, str]] = []
    for item in data["folders"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return [], "unknown_schema"
        entries.append({"id": entry_id(item["path"]), "path": item["path"], "marked_at": str(item.get("marked_at", ""))})
    return entries, ""


def load(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The file as the Settings list shows it: every entry with whether it counts now."""
    path = store_path(home)
    entries, problem = _read(path)
    if problem:
        logger.warning("disposable folder list %s adds nothing: %s", path, problem)
    started = {_norm(p) for p in _START.get(_start_key(home), [])}
    shown = []
    for entry in entries:
        reason = refusal_reason(entry["path"])
        shown.append({
            **entry,
            "refused": reason,
            "active": not reason and _norm(entry["path"]) in started,
            "pending_restart": not reason and _norm(entry["path"]) not in started,
            "syncs_to_cloud": in_sync_root(entry["path"]),
        })
    return {"ok": True, "folders": shown, "problem": problem}


def live_folders(home: str | os.PathLike[str] | None = None) -> list[str]:
    """The file's entries that pass every check now ([] on any doubt about the file)."""
    entries, problem = _read(store_path(home))
    if problem:
        return []
    return [e["path"] for e in entries if not refusal_reason(e["path"])]


def _start_key(home: str | os.PathLike[str] | None) -> str:
    """One key per real file, however VOOL home is spelled (case, separators, a linked home)."""
    path = store_path(home)
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def start_folders(home: str | os.PathLike[str] | None = None) -> list[str]:
    """The file's entries as they were at this process's start, shrunk by any unmark since."""
    key = _start_key(home)
    if key not in _START:
        _START[key] = live_folders(home)
        if _START[key]:
            logger.info("disposable folders from %s at start: %s", key, "; ".join(_START[key]))
    live = {_norm(p) for p in live_folders(home)}
    return [p for p in _START[key] if _norm(p) in live]


def unmarked_since_start(home: str | os.PathLike[str] | None = None) -> set[str]:
    """Start entries (normalised, resolved) the live file no longer keeps: they stop counting now."""
    key = _start_key(home)
    live = {_norm(p) for p in live_folders(home)}
    gone: set[str] = set()
    for folder in _START.get(key, []):
        if _norm(folder) not in live:
            gone.add(_norm(folder))
            with contextlib.suppress(OSError, ValueError):
                gone.add(_norm(_real(folder)))
    return gone


def _write(path: Path, entries: list[dict[str, str]]) -> None:
    from core import windows_sharing_retry
    from core.owner_only import ensure_private_dir, require_owner_only

    ensure_private_dir(path.parent)
    payload = {"schema": SCHEMA, "folders": [{"path": e["path"], "marked_at": e["marked_at"]} for e in entries]}
    handle, tmp = tempfile.mkstemp(prefix=".disposable-", suffix=".tmp", dir=str(path.parent))
    try:
        os.close(handle)
        require_owner_only(tmp)
        with open(tmp, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2) + "\n")
        windows_sharing_retry.replace(tmp, str(path))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _audit(event: str, path: str) -> None:
    try:
        from core import audit_logger

        audit_logger.log(event, target_id=entry_id(path), target_type="disposable_folder", details={"path": path})
    except Exception as exc:  # the change itself stands; the log says the record failed
        logger.warning("audit of %s for %s failed (%s)", event, path, type(exc).__name__)


def mark(path: str, *, home: str | os.PathLike[str] | None = None, extra_sealed: tuple[str, ...] = ()) -> dict[str, str]:
    """Add ``path`` (already confirmed by the owner in an OS dialog). Raises MarkRefused."""
    reason = refusal_reason(path, extra_sealed=extra_sealed)
    if reason:
        raise MarkRefused(reason)
    real = _real(path)
    target = store_path(home)
    entries, problem = _read(target)
    if problem:
        raise MarkRefused(f"the saved folder list cannot be trusted ({problem}); nothing was changed")
    if any(_norm(e["path"]) == _norm(real) for e in entries):
        return next(e for e in entries if _norm(e["path"]) == _norm(real))
    entry = {"id": entry_id(real), "path": real, "marked_at": datetime.now(timezone.utc).isoformat()}
    _write(target, [*entries, entry])
    _audit("disposable_folder_marked", real)
    return entry


def unmark(folder_id: str, *, home: str | os.PathLike[str] | None = None) -> bool:
    target = store_path(home)
    entries, problem = _read(target)
    if problem:
        raise MarkRefused(f"the saved folder list cannot be trusted ({problem}); nothing was changed")
    kept = [e for e in entries if e["id"] != str(folder_id)]
    if len(kept) == len(entries):
        return False
    _write(target, kept)
    for gone in (e for e in entries if e["id"] == str(folder_id)):
        _audit("disposable_folder_unmarked", gone["path"])
    return True


def _reset_for_tests() -> None:
    _START.clear()
