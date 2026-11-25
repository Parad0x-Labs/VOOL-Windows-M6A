"""An installer run over an older install leaves no files the new build does not ship.

Installing 0.7.3 over 0.7.2 on Windows left certifi-2026.10.23.dist-info beside the new
certifi-2026.11.20.dist-info in {app}\\python, and importlib.metadata reported the old version. Two
causes: vool.iss copied python\\* over the old folder without clearing it, and build_bundle.ps1
installed the lean runtime unpinned, so every build took that day's newest releases. The installer now
clears the bundle's runtime folders before copying (as the signed updater replaces them whole), and the
build installs only hash-pinned versions, the ones uv.lock pins.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "installer" / "bundle"
ISS = BUNDLE / "vool.iss"
BUILD_PS1 = BUNDLE / "build_bundle.ps1"
RUNTIME_PINS = BUNDLE / "requirements-windows-runtime.txt"
BUILD_PINS = BUNDLE / "requirements-windows-build.txt"
LEAN_MARKER = "& $py -m pip install --no-build-isolation"


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _section(text: str, name: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            inside = line == f"[{name}]"
            continue
        if inside and line and not line.startswith(";"):
            lines.append(line)
    return lines


def _install_delete_entries() -> dict[str, str]:
    """Each [InstallDelete] folder under {app}, mapped to its Check function ("" when it has none)."""
    found: dict[str, str] = {}
    for line in _section(ISS.read_text(encoding="utf-8"), "InstallDelete"):
        match = re.fullmatch(r'Type:\s*filesandordirs;\s*Name:\s*"\{app\}\\([^"]+)"(?:;\s*Check:\s*(\w+))?', line)
        assert match, f"unexpected [InstallDelete] entry: {line}"
        found[match.group(1)] = match.group(2) or ""
    return found


def _install_delete_dirs() -> set[str]:
    return set(_install_delete_entries())


def _pins(path: Path) -> dict[str, tuple[str, list[str]]]:
    text = path.read_text(encoding="utf-8")
    pins: dict[str, tuple[str, list[str]]] = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", "\n".join(l for l in text.splitlines() if not l.startswith("#"))):
        block = block.strip()
        if not block:
            continue
        head = block.split()[0]
        name, _, version = head.partition("==")
        assert version, f"{path.name}: {head} is not pinned with =="
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", block)
        pins[_normalized(name)] = (version, hashes)
    return pins


def _lock_versions() -> dict[str, set[str]]:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    versions: dict[str, set[str]] = {}
    for package in lock["package"]:
        if "version" in package:
            versions.setdefault(_normalized(package["name"]), set()).add(package["version"])
    return versions


def _lean_names() -> set[str]:
    script = BUILD_PS1.read_text(encoding="utf-8")
    body = script.split(LEAN_MARKER, 1)[1].split("if ($LASTEXITCODE", 1)[0]
    body = body.replace("`\r\n", " ").replace("`\n", " ")
    return {_normalized(re.split(r"[<>=!~\[]", t, maxsplit=1)[0]) for t in shlex.split(body) if not t.startswith("-")}


def test_the_installer_clears_the_bundle_runtime_folders_before_copying():
    cleared = _install_delete_dirs()
    assert {"python", "app", "ollama"} <= cleared, cleared
    # The same folders the signed updater swaps whole, and nothing that holds backups or staging.
    from core.updater import windows as updater

    assert {"python", "app", "ollama"} <= set(updater._MANAGED_DIRECTORIES)
    for kept in ("update_backups", ".vool-update", "models"):
        assert kept not in cleared, f"[InstallDelete] must not clear {kept}"


def test_the_install_time_stop_runs_before_any_file_is_touched():
    text = ISS.read_text(encoding="utf-8")
    prepare = text.split("function PrepareToInstall", 1)[1].split("end;", 1)[0]
    assert "StopVoolProcesses();" in prepare, "the runtime folders are cleared while VOOL's python could still run"


def test_every_runtime_pin_is_hashed_and_is_the_locked_version():
    locked = _lock_versions()
    for path in (RUNTIME_PINS, BUILD_PINS):
        pins = _pins(path)
        assert pins, f"{path.name} pins nothing"
        for name, (version, hashes) in pins.items():
            assert hashes, f"{path.name}: {name} has no sha256"
            if path is BUILD_PINS and name not in locked:
                continue  # wheel is a build tool, not a dependency, so uv.lock does not name it
            assert version in locked.get(name, set()), f"{path.name}: {name}=={version} is not the version uv.lock pins"


def test_every_package_the_lean_install_names_is_pinned():
    pins = _pins(RUNTIME_PINS)
    missing = sorted(_lean_names() - set(pins))
    assert not missing, f"named in build_bundle.ps1 but not pinned in {RUNTIME_PINS.name}: {missing}"
    assert {"setuptools", "wheel"} <= set(_pins(BUILD_PINS))


def test_the_build_installs_only_from_the_hash_checked_wheelhouse():
    script = BUILD_PS1.read_text(encoding="utf-8")
    bootstrap = script.split("-r $BuildRequirements", 1)[0].rsplit("& $py -m pip install", 1)[1]
    assert "--require-hashes" in bootstrap and "--only-binary=:all:" in bootstrap
    download = next(line for line in script.splitlines() if "-m pip download" in line)
    assert "--require-hashes" in download and "-r $RuntimeRequirements" in download
    lean = script.split(LEAN_MARKER, 1)[1].split("if ($LASTEXITCODE", 1)[0]
    assert "--no-index" in lean and "--find-links=$Wheelhouse" in lean


def test_the_runtime_folders_are_cleared_only_inside_an_existing_vool_install():
    # The folder page and /DIR= let a person install into a folder of their own, where a "python",
    # "app" or "ollama" folder is theirs: a first install, or one into a folder with no VOOL install,
    # must delete nothing.
    entries = _install_delete_entries()
    for name in ("python", "app", "ollama"):
        assert entries[name] == "IsVoolInstall", f"{{app}}\\{name} is cleared without checking it is a VOOL install"


def test_the_vool_install_check_needs_every_bundle_marker():
    text = ISS.read_text(encoding="utf-8")
    body = text.split("function DetectVoolInstall(): Boolean;", 1)[1].split("\nend;", 1)[0]
    assert "Result := False;" in body.split("begin", 1)[1].splitlines()[1], "the check must start from False"
    for marker in (
        "FileExists(Manifest)",
        "\\bundle_manifest.json'",
        "FileExists(Root + '\\bundle_supervisor.py')",
        "FileExists(Root + '\\python\\python.exe')",
        "DirExists(Root + '\\app\\core')",
    ):
        assert marker in body, f"DetectVoolInstall lost the marker {marker}"
    from core import runtime_paths

    schema = runtime_paths._WINDOWS_BUNDLE_MANIFEST_SCHEMA
    assert f"Pos('{schema}'" in body, "DetectVoolInstall must check the manifest schema the runtime checks"
    assert f'schema = "{schema}"' in BUILD_PS1.read_text(encoding="utf-8"), "the build writes another manifest schema"


def test_the_vool_install_answer_is_recorded_once_before_anything_is_deleted():
    # Setup calls each [InstallDelete] Check as it reaches that entry, and python is cleared first: a Check
    # that re-read the markers would then miss python\python.exe and leave app and ollama uncleared.
    text = ISS.read_text(encoding="utf-8")
    check = text.split("function IsVoolInstall(): Boolean;", 1)[1].split("\nend;", 1)[0]
    assert check.split("begin", 1)[1].strip() == "Result := VoolInstallFound;", (
        "IsVoolInstall must only return the recorded answer"
    )
    prepare = text.split("function PrepareToInstall", 1)[1].split("end;", 1)[0]
    stop, detect = prepare.find("StopVoolProcesses();"), prepare.find("VoolInstallFound := DetectVoolInstall();")
    assert 0 <= stop < detect, (
        "PrepareToInstall must record DetectVoolInstall after the stop, before any file is touched"
    )
    assert text.count("DetectVoolInstall()") == 2, "DetectVoolInstall must be called only from PrepareToInstall"
