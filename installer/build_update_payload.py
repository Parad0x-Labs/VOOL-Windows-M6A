"""Build updater ZIPs from verified Mac apps, a clean Linux release checkout or a Windows bundle stage."""
from __future__ import annotations

import argparse
import ast
import json
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from core.build_provenance import capture_build_source
from core.updater.archive import extract_update_zip
from core.updater.trust import TrustedPublishers


def _tree(source: Path, include: tuple[str, ...] | None):
    if include is None:
        return [source, *sorted(source.rglob("*"))]
    paths = [source]
    for name in include:
        top = source / name
        paths.append(top)
        if top.is_dir() and not top.is_symlink():
            paths.extend(sorted(top.rglob("*")))
    return paths


def write_tree_zip(source: Path, output: Path, *, prefix: str, include: tuple[str, ...] | None = None) -> None:
    """Keep Unix modes and relative symlinks; never follow a link into other data.

    ``include`` limits the archive to those top-level entries of ``source`` (each must exist).
    """
    if output.exists():
        raise ValueError(f"refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in _tree(source, include):
                name = prefix + ("/" + path.relative_to(source).as_posix() if path != source else "")
                mode = path.lstat().st_mode
                info = zipfile.ZipInfo(name + ("/" if stat.S_ISDIR(mode) else ""))
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                if stat.S_ISLNK(mode):
                    archive.writestr(info, path.readlink().as_posix().encode())
                elif stat.S_ISDIR(mode):
                    archive.writestr(info, b"")
                elif stat.S_ISREG(mode):
                    with path.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
                        while block := reader.read(1024 * 1024):
                            writer.write(block)
                else:
                    raise ValueError(f"unsupported payload entry: {name}")
        # Exercise the EXACT production extractor, including link containment,
        # namespace collisions, entry counts and expanded-size limits.
        with tempfile.TemporaryDirectory(prefix=".payload-verify-", dir=output.parent) as temporary:
            extract_update_zip(output, Path(temporary) / "stage")
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _validate_source(source: Path, version: str) -> None:
    tree = ast.parse((source / "core/app_version.py").read_text(encoding="utf-8"))
    declared = next((ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                     and target.id == "VOOL_VERSION" for target in node.targets)), "")
    if declared != version:
        raise ValueError(f"runtime version {declared!r} does not match {version!r}")
    if not TrustedPublishers.load(source / "config/release/trusted_publisher_keys.json", env={}).is_configured:
        raise ValueError("release payload requires a pinned PUBLIC publisher key")


def build_payload(source: Path, output_dir: Path, *, platform: str, version: str) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("manifest/runtime version must be numeric, without -beta")
    source = source.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if platform in ("macos-arm64", "macos-x64"):
        arch = "arm64" if platform == "macos-arm64" else "x86_64"
        resources = source / "Contents/Resources"
        manifest = json.loads((resources / "BUILD_MANIFEST.json").read_text(encoding="utf-8"))
        if (manifest.get("arch") != arch or manifest.get("version") != version + "-beta"
                or not manifest.get("source_tree_clean") or not manifest.get("release")
                or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("exact_sha") or ""))):
            raise ValueError("app is not a clean release build of the requested version/architecture")
        _validate_source(resources / "app", version)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(source)], check=True)
        output = output_dir / f"VOOL-{version}-beta-macos-{arch}-update.zip"
        write_tree_zip(source, output, prefix="VOOL.app")
    elif platform == "linux-x64":
        provenance = capture_build_source(source)
        if provenance.get("source_kind") != "git" or provenance.get("dirty_state"):
            raise ValueError("Linux payload requires a clean committed release checkout")
        _validate_source(source, version)
        if output_dir.is_relative_to(source):
            raise ValueError("put release outputs outside the source checkout")
        output = output_dir / f"vool-{version}-beta-linux-update.zip"
        with tempfile.TemporaryDirectory(prefix=".linux-payload-", dir=output_dir) as temporary:
            root = Path(temporary)
            archive = root / "source.zip"
            subprocess.run(["git", "-C", str(source), "archive", "--format=zip",
                            "--output", str(archive), "HEAD"], check=True)
            stage = root / "stage"
            extract_update_zip(archive, stage)
            (stage / "config/build-source.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
            write_tree_zip(stage, output, prefix=f"vool-{version}-beta")
    elif platform == "windows-x64":
        output = _build_windows_payload(source, output_dir, version=version)
    else:
        raise ValueError(f"unsupported release platform: {platform}")
    return output


def _build_windows_payload(stage: Path, output_dir: Path, *, version: str) -> Path:
    """Zip exactly the entries VOOL-Setup.exe installs into the program folder.

    ``stage`` is build_bundle.ps1's staging folder. Its seed ``models`` folder and downloaded
    archives are never part of an update: models live in the user's store, not the program folder.
    """
    from core.updater.windows import MANAGED_ENTRIES, WindowsBundleInstaller, payload_identity_problem

    problem = payload_identity_problem(stage, version=version, require_commit=False)
    if problem:
        raise ValueError(problem)
    from core.updater.windows import read_bundle_manifest

    commit = str(read_bundle_manifest(stage).get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("the Windows stage does not record its exact source commit")
    for name in MANAGED_ENTRIES:
        if not (stage / name).exists() or (stage / name).is_symlink():
            raise ValueError(f"the Windows stage is missing {name}")
    _validate_source(stage / "app", version)
    if output_dir.is_relative_to(stage):
        raise ValueError("put release outputs outside the stage folder")
    output = output_dir / f"VOOL-{version}-windows-x64-update.zip"
    write_tree_zip(stage, output, prefix="VOOL", include=MANAGED_ENTRIES)
    with tempfile.TemporaryDirectory(prefix=".windows-payload-verify-", dir=output_dir) as temporary:
        bundle = extract_update_zip(output, Path(temporary) / "stage")
        verification = WindowsBundleInstaller().verify_bundle(bundle)
        if not verification.ok:
            output.unlink(missing_ok=True)
            raise ValueError(f"the Windows payload failed its own installer check: {verification.detail}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "macos-x64", "linux-x64", "windows-x64"))
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    print(build_payload(args.source, args.out, platform=args.platform, version=args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
