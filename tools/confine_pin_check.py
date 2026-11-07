"""Check the shipped VoolConfine launcher folder against its pin, with negative controls.

Run by tools/windows_install_smoke.ps1 with the INSTALLED python, so it checks the installed
sandbox/windows_confinement_pin.py against the installed files:

    <app>\\python\\python.exe -I tools\\confine_pin_check.py --app-root <app>\\app --work <empty dir> --out <json>

Every check is PASS or FAIL in the JSON; the exit code is 0 only when all pass.
  installed_launcher_verified   the installed folder matches its pin
  copy_verified                 a byte-for-byte copy matches too (positive control for the rest)
  extra_file_refused            a DLL planted in the copy is refused
  changed_byte_refused          one flipped byte in the copy's VoolConfine.exe is refused
  missing_file_refused          a pinned file deleted from the copy is refused
  missing_pin_refused           the copy without its pin is refused
  held_file_not_writable        Windows: while verified, a pinned file can't be opened for writing
  held_file_not_renamable       Windows: while verified, a pinned file can't be renamed away
  swapped_folder_refused        a tampered folder moved in under the verified path is refused
The copy lives under --work and never touches the installed files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.app_root))
    from sandbox import windows_confinement_pin as pin

    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    installed = args.app_root.parent
    folder = installed / "broker" / pin.FOLDER_NAME
    pin_path = installed / "config" / pin.PIN_NAME
    expected = folder / pin.LAUNCHER_NAME
    got = pin.verified_launcher(args.app_root)
    files = pin.read_pin(pin_path) or {}
    check("installed_launcher_verified", got == expected, f"{got}; {len(files)} files pinned")
    pin.release_all()

    root = args.work / "copy"
    app = root / "app"
    app.mkdir(parents=True)
    copy_folder = root / "broker" / pin.FOLDER_NAME
    copy_pin = root / "config" / pin.PIN_NAME
    shutil.copytree(folder, copy_folder)
    copy_pin.parent.mkdir(parents=True)
    shutil.copyfile(pin_path, copy_pin)
    exe = copy_folder / pin.LAUNCHER_NAME
    check("copy_verified", pin.verified_launcher(app) == exe)

    planted = copy_folder / "version.dll"
    pin.release_all()
    planted.write_bytes(b"MZ planted")
    check("extra_file_refused", pin.verified_launcher(app) is None)
    planted.unlink()

    original = exe.read_bytes()
    exe.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
    check("changed_byte_refused", pin.verified_launcher(app) is None)
    exe.write_bytes(original)

    victim = next((copy_folder / rel for rel in sorted(files) if rel != pin.LAUNCHER_NAME), None)
    if victim is None:
        check("missing_file_refused", False, "the pin lists only VoolConfine.exe")
    else:
        saved = victim.read_bytes()
        victim.unlink()
        check("missing_file_refused", pin.verified_launcher(app) is None, victim.name)
        victim.write_bytes(saved)

    copy_pin.rename(copy_pin.with_suffix(".off"))
    check("missing_pin_refused", pin.verified_launcher(app) is None)
    copy_pin.with_suffix(".off").rename(copy_pin)

    if pin.verified_launcher(app) != exe:
        check("copy_restored", False, "the restored copy no longer verifies")
    elif os.name == "nt":
        try:
            with open(exe, "r+b"):
                pass
            check("held_file_not_writable", False, "opened for writing while held")
        except PermissionError as exc:
            check("held_file_not_writable", True, str(exc))
        try:
            os.replace(exe, exe.with_suffix(".moved"))
            os.replace(exe.with_suffix(".moved"), exe)
            check("held_file_not_renamable", False, "renamed while held")
        except PermissionError as exc:
            check("held_file_not_renamable", True, str(exc))

    # Swap the whole folder under the verified path for a tampered one. Windows may refuse to move a
    # folder whose files are held open (recorded); either way the tampered folder must be refused.
    tampered = root / "tampered"
    pin.release_all()
    shutil.copytree(copy_folder, tampered)
    (tampered / pin.LAUNCHER_NAME).write_bytes(b"MZ tampered")
    pin.verified_launcher(app)
    moved = "folder moved while held"
    try:
        copy_folder.rename(root / "broker" / "confine.old")
    except PermissionError as exc:
        moved = f"Windows refused to move the held folder: {exc}"
        pin.release_all()
        copy_folder.rename(root / "broker" / "confine.old")
    tampered.rename(copy_folder)
    check("swapped_folder_refused", pin.verified_launcher(app) is None, moved)
    pin.release_all()

    passed = all(c["status"] == "PASS" for c in checks)
    args.out.write_text(json.dumps({"schema": "vool.confine_pin_check.v1", "passed": passed, "checks": checks}, indent=2), encoding="utf-8")
    for c in checks:
        print(f"[{c['status']}] {c['name']} {c['detail']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
