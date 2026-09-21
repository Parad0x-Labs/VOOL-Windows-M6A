"""Create immutable read-only transfer media for a VOOL Windows candidate.

This packages an already-built installer with its exact hash and bundle identity.
It never signs, installs, launches, or mutates the candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signature-status", required=True, choices=("NotSigned", "Valid"))
    args = parser.parse_args()

    installer = args.installer.resolve(strict=True)
    bundle_manifest_path = args.bundle_manifest.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing ISO: {output}")

    bundle = json.loads(bundle_manifest_path.read_text(encoding="utf-8-sig"))
    is_release = bool(bundle.get("release"))
    if is_release and args.signature_status != "Valid":
        raise SystemExit("a release candidate must have a valid signature")
    if not is_release and "internal" not in installer.name.lower():
        raise SystemExit("a non-release installer must be visibly named internal")

    installer_hash = _sha256(installer)
    candidate = {
        "schema": "vool.windows_candidate.v1",
        "release": is_release,
        "build_mode": bundle.get("build_mode"),
        "version": bundle.get("app_version"),
        "source_commit": bundle.get("source_commit_full"),
        "source_dirty_state": bool(bundle.get("source_dirty_state")),
        "installer": installer.name,
        "installer_bytes": installer.stat().st_size,
        "installer_sha256": installer_hash,
        "authenticode_status": args.signature_status,
        "bundle_manifest_sha256": _sha256(bundle_manifest_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "internal-clean-vm-packaging-test" if not is_release else "release-qualification",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata_dir = output.parent / f"{output.stem}-metadata"
    if metadata_dir.exists():
        raise SystemExit(f"refusing to overwrite existing metadata: {metadata_dir}")
    metadata_dir.mkdir()
    manifest_out = metadata_dir / "candidate-manifest.json"
    hashes_out = metadata_dir / "SHA256SUMS.txt"
    manifest_out.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes_out.write_text(f"{installer_hash}  {installer.name}\n", encoding="ascii")

    try:
        import pycdlib
    except ImportError as exc:
        raise SystemExit("pycdlib is required to create read-only ISO transfer media") from exc

    temporary = output.with_suffix(output.suffix + ".new")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="VOOL_INTERNAL" if not is_release else "VOOL_RELEASE")
    try:
        iso.add_file(str(installer), iso_path="/VOOL_SETUP.EXE;1", joliet_path=f"/{installer.name}")
        iso.add_file(str(manifest_out), iso_path="/MANIFEST.JSON;1", joliet_path="/candidate-manifest.json")
        iso.add_file(str(hashes_out), iso_path="/SHA256.TXT;1", joliet_path="/SHA256SUMS.txt")
        iso.write(str(temporary))
    finally:
        iso.close()
    os.replace(temporary, output)
    print(json.dumps({"iso": str(output), "iso_sha256": _sha256(output), **candidate}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
