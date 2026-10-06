"""Python-only Inno upgrade transaction, run by an installer-owned stdlib bootstrap.

No deletion or legacy ownership guessing. Recovery trees are retained deliberately;
unknown/changed installed Python files require review, not automatic cleanup.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path, PureWindowsPath

OWNERSHIP = "python_payload.json"
ACTIVE = ".python-upgrade-v1"
SCHEMA = "vool.python_payload.v1"
MUTEX_NAMES = (r"Local\VOOL_BUNDLE_SUPERVISOR", r"Local\VOOL_WINDOW_SINGLETON")


class UpgradeError(RuntimeError):
    pass


def encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpgradeError("Duplicate manifest key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    except (ValueError, OSError) as exc:
        raise UpgradeError("Missing or invalid ownership/recovery inventory") from exc
    if not isinstance(value, dict):
        raise UpgradeError("Invalid manifest object")
    return value


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path: Path) -> None:
    """lstat catches Windows junctions even on Python without Path.is_junction."""
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400 or
            not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or
            (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
        raise UpgradeError("Reparse point, hard link or non-regular filesystem object refused")


def safe_path(path: Path) -> None:
    if not path.is_absolute():
        raise UpgradeError("Absolute local path required")
    if os.name == "nt":
        windows = PureWindowsPath(path)
        if not re.fullmatch(r"[A-Za-z]:", windows.drive) or windows.root != "\\":
            raise UpgradeError("UNC/device paths refused")
    for part in path.parts[1:]:
        if part in (".", "..") or part.endswith((".", " ")) or ":" in part:
            raise UpgradeError("Ambiguous path refused")
    for parent in reversed((path, *path.parents)):
        if parent.exists() or parent.is_symlink():
            regular(parent)
    if path.resolve() != path:
        raise UpgradeError("Redirected path refused")


def validate_manifest(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema", "bundle_manifest_sha256", "files"} or
            value["schema"] != SCHEMA or
            not isinstance(value["bundle_manifest_sha256"], str) or
            not re.fullmatch(r"[0-9a-f]{64}", value["bundle_manifest_sha256"]) or
            not isinstance(value["files"], dict) or not value["files"]):
        raise UpgradeError("Invalid Python payload inventory")
    seen = set()
    for relative, item in value["files"].items():
        if not isinstance(relative, str):
            raise UpgradeError("Invalid inventory path")
        parts = relative.split("/")
        if (any(not p or p in (".", "..") or p.endswith((".", " ")) or
                re.search(r'[\\:<>"|?*\x00-\x1f]', p) or PureWindowsPath(p).is_reserved()
                for p in parts) or relative.casefold() in seen):
            raise UpgradeError("Unsafe or aliased inventory path")
        seen.add(relative.casefold())
        if (not isinstance(item, dict) or set(item) != {"size", "sha256"} or
                type(item["size"]) is not int or item["size"] < 0 or
                not isinstance(item["sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise UpgradeError("Invalid file identity")
    if "python.exe" not in value["files"]:
        raise UpgradeError("Python entrypoint missing from inventory")
    return value


def files_in(directory: Path) -> dict:
    regular(directory)
    if not directory.is_dir():
        raise UpgradeError("Python payload must be a regular directory")
    result = {}

    def walk(parent):
        for child in sorted(parent.iterdir()):
            regular(child)
            if child.is_dir():
                walk(child)
            else:
                result[child.relative_to(directory).as_posix()] = {
                    "size": child.stat().st_size, "sha256": digest(child),
                }

    walk(directory)
    return result


def inventory(root: Path) -> dict:
    safe_path(root)
    regular(root / "bundle_manifest.json")
    bundle = read_json(root / "bundle_manifest.json")
    if (bundle.get("schema") != "vool.bundle_manifest.v1" or
            not isinstance(bundle.get("source_commit_full"), str) or
            not re.fullmatch(r"[0-9a-f]{40}", bundle["source_commit_full"])):
        raise UpgradeError("Valid VOOL bundle/source identity required")
    return validate_manifest({
        "schema": SCHEMA,
        "bundle_manifest_sha256": digest(root / "bundle_manifest.json"),
        "files": files_in(root / "python"),
    })


def runtime_running() -> bool:
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    for name in MUTEX_NAMES:
        ctypes.set_last_error(0)
        handle = kernel.OpenMutexW(0x00100000, False, name)
        if handle:
            kernel.CloseHandle(handle)
            return True
        if ctypes.get_last_error() != 2:  # only ERROR_FILE_NOT_FOUND proves absence
            raise UpgradeError("Unable to verify VOOL runtime mutex absence")
    return False


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("xb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Transaction:
    def __init__(self, root: Path, programs: Path, home: Path, incoming: dict):
        self.root, self.programs, self.home = root, programs, home
        self.incoming = validate_manifest(incoming)
        self.active = root / ACTIVE

    def _guard(self):
        for path in (self.root, self.programs, self.home):
            safe_path(path)
        if self.root != self.programs / "VOOL":
            raise UpgradeError("Only the canonical per-user Programs/VOOL root is supported")
        if (self.root == self.home or self.root.is_relative_to(self.home) or
                self.home.is_relative_to(self.root) or self.programs.is_relative_to(self.home)):
            raise UpgradeError("Program/profile overlap refused")
        if any(part.casefold() == "nulla" for part in self.root.parts):
            raise UpgradeError("Legacy NULLA root refused")
        for marker in (self.root / OWNERSHIP, self.root / "bundle_manifest.json"):
            if os.path.lexists(marker):
                regular(marker)  # includes dangling ownership links after uninstall
        if os.path.lexists(self.root / (OWNERSHIP + ".pending")):
            raise UpgradeError("Interrupted metadata write; manual recovery review required")
        if runtime_running():
            raise UpgradeError("Close VOOL completely before upgrading Python")

    def check(self):
        self._guard()
        if self.active.exists():
            raise UpgradeError("Pending Python transaction requires recovery before installation")
        python = self.root / "python"
        if python.exists():
            safe_path(self.root / OWNERSHIP)
            old = validate_manifest(read_json(self.root / OWNERSHIP))
            if inventory(self.root) != old:
                raise UpgradeError("Unowned, changed or build-mismatched Python payload; preserved for review")
        elif self.root.exists():
            # Uninstall deliberately retains verified recovery material. Reinstall may
            # coexist with that material, never with arbitrary program/profile residue.
            ownership = None
            if os.path.lexists(self.root / OWNERSHIP):
                safe_path(self.root / OWNERSHIP)
                ownership = validate_manifest(read_json(self.root / OWNERSHIP))
            identities = []
            for child in self.root.iterdir():
                if child.name == OWNERSHIP:
                    continue
                if not re.fullmatch(r"\.python-recovery-[0-9a-f]{32}", child.name):
                    raise UpgradeError("Existing installation without owned Python payload; preserved for review")
                journal = self._journal(child)
                if journal["state"] not in ("committed", "restored"):
                    raise UpgradeError("Incomplete retained recovery material; review required")
                identities.append(journal["incoming"] if journal["state"] == "committed" else journal["prior_ownership"])
            if ownership is not None and ownership not in identities:
                raise UpgradeError("Unbound retained ownership marker; preserved for review")

    def _save(self, journal):
        atomic_json(self.active / "journal.json", journal)

    def _journal(self, directory=None):
        directory = self.active if directory is None else directory
        safe_path(directory)
        # Reject redirected entries anywhere in the recovery tree before rename/copy.
        for parent, directories, files in os.walk(directory, followlinks=False):
            for name in directories + files:
                regular(Path(parent) / name)
        if any(directory.glob("*.pending")) or any(directory.glob("*.restore")):
            raise UpgradeError("Interrupted metadata write; manual recovery review required")
        journal = read_json(directory / "journal.json")
        if (set(journal) != {"schema", "root", "state", "old", "prior_ownership", "incoming", "recovery_id"} or
                journal["schema"] != "vool.python_upgrade.v1" or journal["root"] != str(self.root) or
                journal["state"] not in ("prepared", "restoring", "restored", "committed") or
                not isinstance(journal["recovery_id"], str) or
                not re.fullmatch(r"[0-9a-f]{32}", journal["recovery_id"])):
            raise UpgradeError("Invalid recovery journal; preserve for manual review")
        if directory != self.active and directory.name != ".python-recovery-" + journal["recovery_id"]:
            raise UpgradeError("Recovery archive identity mismatch")
        validate_manifest(journal["incoming"])
        if journal["prior_ownership"] is not None:
            validate_manifest(journal["prior_ownership"])
        if journal["old"] is not None:
            validate_manifest(journal["old"])
            if journal["old"] != journal["prior_ownership"]:
                raise UpgradeError("Previous ownership binding mismatch")
        return journal

    def begin(self):
        self.check()  # recheck immediately before retirement, not just in PrepareToInstall
        old = read_json(self.root / OWNERSHIP) if (self.root / "python").exists() else None
        self.root.mkdir(parents=True, exist_ok=True)
        self.active.mkdir()  # exclusive; no overwrite of a previous transaction
        journal = {
            "schema": "vool.python_upgrade.v1", "root": str(self.root), "state": "prepared",
            "old": old, "prior_ownership": read_json(self.root / OWNERSHIP) if (self.root / OWNERSHIP).exists() else None,
            "incoming": self.incoming, "recovery_id": uuid.uuid4().hex,
        }
        self._save(journal)  # durable intent BEFORE the same-volume rename
        if journal["prior_ownership"] is not None:
            (self.active / "old-ownership.json").write_bytes((self.root / OWNERSHIP).read_bytes())
        if old is not None:
            (self.active / "old-bundle-manifest.json").write_bytes((self.root / "bundle_manifest.json").read_bytes())
            os.replace(self.root / "python", self.active / "previous")

    def verify(self):
        self._guard()
        journal = self._journal()
        if journal["state"] != "prepared" or journal["incoming"] != self.incoming:
            raise UpgradeError("Candidate/transaction identity mismatch")
        if inventory(self.root) != self.incoming:
            raise UpgradeError("Extracted Python payload does not exactly match candidate hashes")

    def _archive(self):
        journal = self._journal()
        target = self.root / (".python-recovery-" + journal["recovery_id"])
        if target.exists():
            raise UpgradeError("Recovery archive already exists; no overwrite")
        os.replace(self.active, target)
        return target

    def finish(self):
        # Called ONLY after the existing legacy-state migration succeeds.
        self.verify()
        journal = self._journal()
        atomic_json(self.root / OWNERSHIP, self.incoming)
        journal["state"] = "committed"
        self._save(journal)
        return self._archive()  # retained rollback evidence, not automatic deletion

    def recover(self):
        self._guard()
        if not self.active.exists():
            return None
        journal = self._journal()
        old = journal["old"]
        if journal["state"] == "committed":
            if (inventory(self.root) != journal["incoming"] or
                    read_json(self.root / OWNERSHIP) != journal["incoming"]):
                raise UpgradeError("Committed recovery identity mismatch; preserve for review")
            return self._archive()
        previous = self.active / "previous"
        python = self.root / "python"
        if previous.exists():
            if old is None or files_in(previous) != old["files"]:
                raise UpgradeError("Previous payload changed; recovery refused")
            old_manifest = self.active / "old-bundle-manifest.json"
            if digest(old_manifest) != old["bundle_manifest_sha256"]:
                raise UpgradeError("Previous build binding changed; recovery refused")
            if read_json(self.active / "old-ownership.json") != old:
                raise UpgradeError("Previous ownership changed; recovery refused")
            journal["state"] = "restoring"
            self._save(journal)
            if python.exists():
                files_in(python)  # no reparse traversal; even partial extraction is retained
                if (self.active / "failed").exists():
                    raise UpgradeError("Ambiguous failed extraction; preserve for review")
                os.replace(python, self.active / "failed")
            os.replace(previous, python)
        elif old is not None and (not python.exists() or files_in(python) != old["files"]):
            raise UpgradeError("Previous payload unavailable; preserve for manual recovery")
        elif old is None and python.exists():
            files_in(python)
            if (self.active / "failed").exists():
                raise UpgradeError("Ambiguous fresh extraction; preserve for review")
            os.replace(python, self.active / "failed")
        # Restore markers too; app/native/Ollama rollback remains Inno's responsibility.
        markers = []
        if journal["prior_ownership"] is not None:
            markers.append(("old-ownership.json", OWNERSHIP))
        if old is not None:
            markers.append(("old-bundle-manifest.json", "bundle_manifest.json"))
        for saved, target in markers:
            source = self.active / saved
            if source.exists():
                if saved == "old-ownership.json" and read_json(source) != journal["prior_ownership"]:
                    raise UpgradeError("Retained ownership changed; recovery refused")
                safe_path(self.root / target)
                temporary = self.active / (saved + ".restore")
                with temporary.open("xb") as stream:
                    stream.write(source.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.root / target)
        if journal["prior_ownership"] is None and (self.root / OWNERSHIP).exists():
            if read_json(self.root / OWNERSHIP) != journal["incoming"]:
                raise UpgradeError("Unowned ownership marker; preserve for review")
            os.replace(self.root / OWNERSHIP, self.active / "failed-ownership.json")
        journal["state"] = "restored"
        self._save(journal)
        return self._archive()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "check", "begin", "verify", "finish", "recover"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--programs", type=Path)
    parser.add_argument("--home", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "inventory":
            with args.manifest.open("xb") as output:
                output.write(encode(inventory(args.root)))
        else:
            if args.programs is None or args.home is None:
                raise UpgradeError("Canonical program/profile boundaries required")
            operation = Transaction(args.root, args.programs, args.home, read_json(args.manifest))
            result = getattr(operation, args.action)()
            if result is not None:
                print("Python recovery material retained at:", result)
        return 0
    except (UpgradeError, OSError) as exc:
        print("Python payload upgrade refused:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
