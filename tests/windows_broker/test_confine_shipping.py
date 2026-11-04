"""The job launcher VoolConfine ships in its own pinned folder, and VOOL refuses it unless that
folder holds exactly the pinned files (docs/WINDOWS_CONFINEMENT.md section 2)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox import windows_confinement as wc
from sandbox import windows_confinement_pin as pin

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _release_held_files():
    yield
    pin.release_all()


# --- a launcher folder on disk ---------------------------------------------------------------

FILES = {
    "VoolConfine.exe": b"MZ launcher",
    "VoolConfine.dll": b"launcher code",
    "VoolConfine.Core.dll": b"core code",
    "coreclr.dll": b"runtime",
    "sub/resource.dll": b"satellite",
}


def _write_pin(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": pin.PIN_SCHEMA, "files": files}), encoding="utf-8")


def _install(tmp_path: Path, *, checkout: bool = False) -> tuple[Path, Path, Path]:
    """An app root, its launcher folder and its pin, laid out as installed (or as a checkout)."""
    if checkout:
        app = tmp_path / "repo"
        folder = app / "windows" / "broker" / "publish" / "confine"
        pin_path = app / "windows" / "broker" / "publish" / "confine-pin.json"
    else:
        app = tmp_path / "VOOL" / "app"
        folder = tmp_path / "VOOL" / "broker" / "confine"
        pin_path = tmp_path / "VOOL" / "config" / "confine-pin.json"
    app.mkdir(parents=True, exist_ok=True)
    for rel, data in FILES.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _write_pin(pin_path, {rel: hashlib.sha256(data).hexdigest() for rel, data in FILES.items()})
    return app, folder, pin_path


def test_an_installed_folder_that_matches_its_pin_is_the_launcher(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    assert pin.verified_launcher(app) == folder / "VoolConfine.exe"
    # launcher_path (chunk 0's entry point) delegates here.
    assert wc.launcher_path(app) == folder / "VoolConfine.exe"
    # Asking again is the same answer (on Windows, from the held files).
    assert pin.verified_launcher(app) == folder / "VoolConfine.exe"


def test_a_checkout_publish_that_matches_its_pin_is_the_launcher(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path, checkout=True)
    assert pin.verified_launcher(app) == folder / "VoolConfine.exe"


def test_no_launcher_anywhere_means_unavailable(tmp_path: Path) -> None:
    (tmp_path / "VOOL" / "app").mkdir(parents=True)
    assert pin.verified_launcher(tmp_path / "VOOL" / "app") is None


def test_a_planted_file_is_refused(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    (folder / "version.dll").write_bytes(b"MZ planted")
    assert pin.verified_launcher(app) is None


def test_a_file_planted_after_verification_is_refused(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    assert pin.verified_launcher(app) is not None
    (folder / "sub" / "version.dll").write_bytes(b"MZ planted")
    assert pin.verified_launcher(app) is None


def test_an_empty_folder_holds_no_code_but_a_file_planted_in_it_is_refused(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    (folder / "empty").mkdir()
    assert pin.verified_launcher(app) == folder / "VoolConfine.exe"
    (folder / "empty" / "x.dll").write_bytes(b"MZ")
    assert pin.verified_launcher(app) is None


@pytest.mark.parametrize("rel", sorted(FILES))
def test_one_changed_byte_in_any_pinned_file_is_refused(tmp_path: Path, rel: str) -> None:
    app, folder, _ = _install(tmp_path)
    target = folder / rel
    data = target.read_bytes()
    target.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    assert pin.verified_launcher(app) is None


@pytest.mark.parametrize("rel", sorted(FILES))
def test_a_missing_pinned_file_is_refused(tmp_path: Path, rel: str) -> None:
    app, folder, _ = _install(tmp_path)
    (folder / rel).unlink()
    assert pin.verified_launcher(app) is None


def test_a_file_changed_after_verification_is_refused_or_cannot_be_changed(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    assert pin.verified_launcher(app) is not None
    if os.name == "nt":
        # Held with FILE_SHARE_READ only: the change itself is refused, and the launcher stays good.
        with pytest.raises(PermissionError):
            (folder / "VoolConfine.dll").write_bytes(b"other code")
        assert pin.verified_launcher(app) == folder / "VoolConfine.exe"
    else:
        # Nothing can be held here, so the next call re-hashes and refuses the change.
        (folder / "VoolConfine.dll").write_bytes(b"other code")
        assert pin.verified_launcher(app) is None


def test_a_missing_pin_is_refused(tmp_path: Path) -> None:
    app, _, pin_path = _install(tmp_path)
    pin_path.unlink()
    assert pin.verified_launcher(app) is None


def test_the_installed_launcher_never_falls_back_to_the_checkout_one(tmp_path: Path) -> None:
    # An installed folder that fails is refused even if another location would pass.
    app = tmp_path / "VOOL" / "app"
    _install(tmp_path, checkout=False)
    checkout_folder = app / "windows" / "broker" / "publish" / "confine"
    for rel, data in FILES.items():
        (checkout_folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (checkout_folder / rel).write_bytes(data)
    _write_pin(checkout_folder.parent / "confine-pin.json", {r: hashlib.sha256(d).hexdigest() for r, d in FILES.items()})
    (tmp_path / "VOOL" / "broker" / "confine" / "version.dll").write_bytes(b"MZ planted")
    assert pin.verified_launcher(app) is None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


GOOD_PIN = {rel: _sha(data) for rel, data in FILES.items()}


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        json.dumps({"files": GOOD_PIN}),
        json.dumps({"schema": "vool.windows_confinement.pin.v2", "files": GOOD_PIN}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": GOOD_PIN, "extra": 1}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": []}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": {k: v for k, v in GOOD_PIN.items() if k != "VoolConfine.exe"}}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": {**GOOD_PIN, "VoolConfine.exe": GOOD_PIN["VoolConfine.exe"].upper()}}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": {**GOOD_PIN, "VoolConfine.exe": "ab"}}),
        json.dumps({"schema": pin.PIN_SCHEMA, "files": {**GOOD_PIN, "VoolConfine.exe": 7}}),
        "\ufeff" + json.dumps({"schema": pin.PIN_SCHEMA, "files": GOOD_PIN}),
    ],
)
def test_a_malformed_pin_is_refused(tmp_path: Path, body: str) -> None:
    app, _, pin_path = _install(tmp_path)
    pin_path.write_text(body, encoding="utf-8")
    assert pin.verified_launcher(app) is None


@pytest.mark.parametrize(
    "key",
    [
        "/VoolConfine.dll",
        "sub\\resource.dll",
        "../outside.dll",
        "./VoolConfine.dll",
        "sub//resource.dll",
        "VoolConfine.dll.",
        "VoolConfine.dll ",
        "VoolConfine.dll:stream",
        "C:/x.dll",
        "sub/",
        "a\x00b",
    ],
)
def test_a_pin_key_that_could_alias_another_name_is_refused(tmp_path: Path, key: str) -> None:
    app, _, pin_path = _install(tmp_path)
    _write_pin(pin_path, {**GOOD_PIN, key: _sha(b"x")})
    assert pin.read_pin(pin_path) is None
    assert pin.verified_launcher(app) is None


def test_two_pin_keys_for_one_windows_name_are_refused(tmp_path: Path) -> None:
    app, _, pin_path = _install(tmp_path)
    _write_pin(pin_path, {**GOOD_PIN, "voolconfine.dll": GOOD_PIN["VoolConfine.dll"]})
    assert pin.read_pin(pin_path) is None
    assert pin.verified_launcher(app) is None


def test_names_are_compared_as_windows_compares_them(tmp_path: Path) -> None:
    app, folder, pin_path = _install(tmp_path)
    _write_pin(pin_path, {(k.lower() if k != "VoolConfine.exe" else k): v for k, v in GOOD_PIN.items()})
    assert pin.verified_launcher(app) == folder / "VoolConfine.exe"


def _link(link: Path, target_dir: Path) -> None:
    """A reparse point at `link`: a junction on Windows (no privilege needed), a symlink elsewhere.
    The target is a directory, because a junction can only point at one."""
    target_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target_dir)], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
    else:
        link.symlink_to(target_dir, target_is_directory=True)


def test_a_linked_pinned_file_is_refused_never_followed(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    (folder / "VoolConfine.dll").unlink()
    _link(folder / "VoolConfine.dll", tmp_path / "elsewhere")
    assert pin.verified_launcher(app) is None


def test_a_linked_subfolder_is_refused_never_followed(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    moved = tmp_path / "real-sub"
    (folder / "sub").rename(moved)
    _link(folder / "sub", moved)
    assert pin.verified_launcher(app) is None


def test_a_linked_launcher_folder_is_refused_never_followed(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    real_folder = tmp_path / "real-confine"
    folder.rename(real_folder)
    _link(folder, real_folder)
    assert pin.verified_launcher(app) is None


def test_a_linked_pin_is_refused_never_followed(tmp_path: Path) -> None:
    app, _, pin_path = _install(tmp_path)
    pin_path.unlink()
    _link(pin_path, tmp_path / "elsewhere")
    assert pin.verified_launcher(app) is None


# --- Windows: the verified files are held -----------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="share-mode locks are Windows-only")
def test_on_windows_a_verified_file_cannot_be_written_renamed_or_deleted(tmp_path: Path) -> None:
    app, folder, _ = _install(tmp_path)
    exe = folder / "VoolConfine.exe"
    assert pin.verified_launcher(app) == exe
    with pytest.raises(PermissionError):
        open(folder / "VoolConfine.dll", "r+b").close()
    with pytest.raises(PermissionError):
        os.replace(exe, folder / "moved.exe")
    with pytest.raises(PermissionError):
        (folder / "coreclr.dll").unlink()
    # The held files stay readable, which is all CreateProcess and the loader need.
    assert exe.read_bytes() == FILES["VoolConfine.exe"]
    pin.release_all()
    os.replace(exe, folder / "moved.exe")
    assert pin.verified_launcher(app) is None


@pytest.mark.skipif(os.name != "nt", reason="share-mode locks are Windows-only")
def test_on_windows_a_held_launcher_still_starts(tmp_path: Path) -> None:
    # A real executable in the pinned folder: the system's cmd.exe, so the test needs no build.
    app = tmp_path / "VOOL" / "app"
    app.mkdir(parents=True)
    folder = tmp_path / "VOOL" / "broker" / "confine"
    folder.mkdir(parents=True)
    exe = folder / "VoolConfine.exe"
    exe.write_bytes((Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe").read_bytes())
    _write_pin(tmp_path / "VOOL" / "config" / "confine-pin.json", {"VoolConfine.exe": _sha(exe.read_bytes())})
    assert pin.verified_launcher(app) == exe
    done = subprocess.run([str(exe), "/d", "/c", "exit 7"], capture_output=True, timeout=60)
    assert done.returncode == 7
    # Still held after the start: the running image did not need write or delete access.
    with pytest.raises(PermissionError):
        open(exe, "r+b").close()


# --- the build and the installer ship it ------------------------------------------------------


def test_publish_ships_voolconfine_self_contained_in_its_own_folder() -> None:
    script = _read("windows/broker/publish.ps1")
    assert '$confineProject = Join-Path $PSScriptRoot "src\\VoolConfine\\VoolConfine.csproj"' in script
    assert '$confineDir = Join-Path $Output "confine"' in script
    # The same pinned runtime as the broker, self-contained.
    assert '"-r", "win-x64"' in script and '"--self-contained"' in script
    assert '"-p:RuntimeFrameworkVersion=10.0.13"' in script
    assert '$confineArgs = @("publish", $confineProject) + $runtimeArgs + @("-p:StartupHookSupport=false", "-o", $confineDir)' in script
    assert 'throw "publish produced no VoolConfine.exe in $confineDir"' in script
    assert 'throw "VoolConfine publish in $confineDir is not self-contained (no coreclr.dll)"' in script


def test_publish_pins_every_file_and_refuses_links() -> None:
    script = _read("windows/broker/publish.ps1")
    assert "Get-ChildItem -LiteralPath $confineRoot -Recurse -Force" in script
    assert "[System.IO.FileAttributes]::ReparsePoint" in script
    assert '(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()' in script
    assert f'schema = "{pin.PIN_SCHEMA}"' in script
    assert 'if (-not $PinPath) { $PinPath = Join-Path $Output "confine-pin.json" }' in script
    # A stale pin is removed before the publish, and a pin inside the folder is refused.
    assert "if (Test-Path -LiteralPath $PinPath) { Remove-Item -LiteralPath $PinPath -Force }" in script
    assert "where it would be an unlisted file" in script
    # No byte-order mark: read_pin decodes strict UTF-8 JSON.
    assert "[System.Text.UTF8Encoding]::new($false)" in script


def test_the_bundle_puts_the_pin_in_config_and_checks_the_staged_folder() -> None:
    script = _read("installer/bundle/build_bundle.ps1")
    assert '$confinePin = Join-Path $Stage "config\\confine-pin.json"' in script
    assert '-Output $brokerDir -Dotnet $Dotnet -Source $BrokerPackages -PinPath $confinePin' in script
    assert 'throw "Broker publish produced no confine\\VoolConfine.exe in $brokerDir"' in script
    assert "from sandbox.windows_confinement_pin import verified_launcher" in script
    assert 'throw "The staged VoolConfine folder does not match its pin $confinePin"' in script


def test_the_installer_ships_the_pin_and_clears_the_launcher_folder_on_upgrade() -> None:
    iss = _read("installer/bundle/vool.iss")
    entry = re.search(r'^Source: "\{#Stage\}\\config\\confine-pin\.json"; DestDir: "([^"]+)"; Flags: ([^\r\n]+)$', iss, re.MULTILINE)
    assert entry, "vool.iss does not ship the VoolConfine pin"
    # launcher_locations reads it from <app root>.parent\config, i.e. {app}\config.
    assert entry.group(1) == "{app}\\config"
    assert "skipifsourcedoesntexist" not in entry.group(2)
    section = re.search(r"^\[InstallDelete\]\s*$(.*?)^\[", iss, re.MULTILINE | re.DOTALL)
    assert section, "vool.iss has no [InstallDelete] section"
    assert 'Type: filesandordirs; Name: "{app}\\broker\\confine"' in section.group(1)
    # Nothing outside the program folder is cleared.
    for line in section.group(1).splitlines():
        if line.startswith("Type:"):
            assert 'Name: "{app}\\' in line


def test_the_locations_match_what_the_installer_and_publish_write(tmp_path: Path) -> None:
    app = tmp_path / "VOOL" / "app"
    (installed_folder, installed_pin), (checkout_folder, checkout_pin) = pin.launcher_locations(app)
    assert installed_folder == tmp_path / "VOOL" / "broker" / "confine"
    assert installed_pin == tmp_path / "VOOL" / "config" / "confine-pin.json"
    assert checkout_folder == app / "windows" / "broker" / "publish" / "confine"
    assert checkout_pin == app / "windows" / "broker" / "publish" / "confine-pin.json"


def test_the_install_smoke_checks_the_installed_pin_with_the_installed_python() -> None:
    smoke = _read("tools/windows_install_smoke.ps1")
    assert '$installedPython = Join-Path $InstallRoot "python\\python.exe"' in smoke
    assert '& $installedPython -I (Join-Path $RepoRoot "tools\\confine_pin_check.py") --app-root (Join-Path $InstallRoot "app")' in smoke
    assert 'Require "install" "confine_launcher_pinned" ($pinExit -eq 0)' in smoke
    assert 'Require "uninstall" "confine_launcher_removed"' in smoke
    assert "confine_pin = $confinePinReport" in smoke


def test_the_smoke_pin_check_passes_on_a_good_install_and_its_controls_bite(tmp_path: Path) -> None:
    # The same tool the install smoke runs, against a fake install: every check must PASS.
    _install(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "report.json"
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "confine_pin_check.py"), "--app-root", str(tmp_path / "VOOL" / "app"), "--work", str(work), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert done.returncode == 0, done.stdout + done.stderr
    names = {c["name"]: c["status"] for c in report["checks"]}
    expected = {
        "installed_launcher_verified",
        "copy_verified",
        "extra_file_refused",
        "changed_byte_refused",
        "missing_file_refused",
        "missing_pin_refused",
        "swapped_folder_refused",
    }
    if os.name == "nt":
        expected |= {"held_file_not_writable", "held_file_not_renamable"}
    assert set(names) == expected and set(names.values()) == {"PASS"}
    assert report["passed"] is True


def test_the_smoke_pin_check_fails_on_a_tampered_install(tmp_path: Path) -> None:
    _, folder, _ = _install(tmp_path)
    (folder / "version.dll").write_bytes(b"MZ planted")
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "report.json"
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / "confine_pin_check.py"), "--app-root", str(tmp_path / "VOOL" / "app"), "--work", str(work), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    assert done.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert {c["name"]: c["status"] for c in report["checks"]}["installed_launcher_verified"] == "FAIL"
