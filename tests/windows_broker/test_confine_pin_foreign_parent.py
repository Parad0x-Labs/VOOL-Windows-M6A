"""A checkout never takes its launcher or pin from its parent folder (partial for E2-seams-08#1).

Only an absolute app root named `app` (the installed `{app}\\app`) probes `{app}\\broker` and
`{app}\\config`. The check is by name only: a checkout cloned as `app` still probes its parent,
which stays open in E2-seams-08#1 until a marker inside `{app}\\app` proves an install.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from sandbox import windows_confinement_pin as pin

FILES = {"VoolConfine.exe": b"MZ launcher", "VoolConfine.dll": b"launcher code"}
FOREIGN = {"VoolConfine.exe": b"MZ foreign launcher"}


@pytest.fixture(autouse=True)
def _release_held_files():
    yield
    pin.release_all()


def _populate(folder: Path, pin_path: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        (folder / rel).write_bytes(data)
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": pin.PIN_SCHEMA, "files": {r: hashlib.sha256(d).hexdigest() for r, d in files.items()}}
    pin_path.write_text(json.dumps(body), encoding="utf-8")


def _mark_installed(app: Path) -> None:
    pin.install_marker(app).write_text("VOOL installed app folder", encoding="ascii")   # what the bundle build stages


def _checkout_pair(root: Path) -> tuple[Path, Path]:
    publish = root / "windows" / "broker" / "publish"
    return (publish / "confine", publish / "confine-pin.json")


def _foreign_parent(parent: Path, name: str) -> Path:
    """A launcher and a pin that verifies it, planted in the folder that holds the root `name`."""
    _populate(parent / "broker" / "confine", parent / "config" / "confine-pin.json", FOREIGN)
    root = parent / name
    root.mkdir(parents=True)
    return root


REFUSED = ["vool", "repo", "apps", "myapp", "app.old", "app ", "app.", "_app", "application", "VOOL"]
ACCEPTED = ["app", "App", "APP", "aPp"]


def _base(tmp_path: Path, name: str) -> Path:
    """Win32 drops a trailing space or dot from a folder name; only the \\\\?\\ spelling keeps it, so on
    Windows a row like "app " is built there to really test a folder of that name."""
    if os.name == "nt" and name[-1:] in (" ", "."):
        return Path("\\\\?\\" + str(tmp_path))
    return tmp_path


# --- refused names: only the checkout pair, never the parent -------------------------------------

@pytest.mark.parametrize("name", REFUSED)
def test_a_checkout_root_yields_only_its_own_pair(tmp_path: Path, name: str) -> None:
    root = tmp_path / name
    locations = pin.launcher_locations(root)
    assert locations == (_checkout_pair(root),)
    for folder, pin_path in locations:
        assert root in folder.parents
        assert root in pin_path.parents


@pytest.mark.parametrize("name", REFUSED)
def test_a_checkout_without_its_own_publish_never_uses_the_parent_launcher(tmp_path: Path, name: str) -> None:
    tmp_path = _base(tmp_path, name)
    root = _foreign_parent(tmp_path, name)
    assert pin.verified_launcher(root) is None


@pytest.mark.parametrize("name", REFUSED)
def test_a_checkout_uses_its_own_publish_not_the_parent_launcher(tmp_path: Path, name: str) -> None:
    tmp_path = _base(tmp_path, name)
    root = _foreign_parent(tmp_path, name)
    folder, pin_path = _checkout_pair(root)
    _populate(folder, pin_path, FILES)
    assert pin.verified_launcher(root) == folder / "VoolConfine.exe"


@pytest.mark.parametrize("relative", [Path("app"), Path("APP"), Path("vool"), Path("x") / "app"])
def test_a_relative_root_never_probes_its_parent(relative: Path) -> None:
    # The parent of a relative root is a folder under the current directory, not a proven install.
    assert pin.launcher_locations(relative) == (_checkout_pair(relative),)


def test_a_relative_app_root_ignores_a_launcher_in_the_current_folder(tmp_path: Path, monkeypatch) -> None:
    _foreign_parent(tmp_path, "app")
    monkeypatch.chdir(tmp_path)
    assert pin.verified_launcher(Path("app")) is None


# --- accepted names: the installed layout first, then the checkout pair --------------------------

@pytest.mark.parametrize("name", ACCEPTED)
def test_an_app_root_probes_the_installed_layout_first(tmp_path: Path, name: str) -> None:
    app = tmp_path / "VOOL" / name
    app.mkdir(parents=True)
    _mark_installed(app)
    assert pin.launcher_locations(app) == (
        (tmp_path / "VOOL" / "broker" / "confine", tmp_path / "VOOL" / "config" / "confine-pin.json"),
        _checkout_pair(app),
    )


@pytest.mark.parametrize("name", ACCEPTED)
def test_an_installed_launcher_beside_an_app_root_verifies(tmp_path: Path, name: str) -> None:
    install = tmp_path / "VOOL"
    app = install / name
    app.mkdir(parents=True)
    _mark_installed(app)
    _populate(install / "broker" / "confine", install / "config" / "confine-pin.json", FILES)
    assert pin.verified_launcher(app) == install / "broker" / "confine" / "VoolConfine.exe"


# --- E2-seams-08#1: an `app` folder is the install only when the marker inside it says so --------

@pytest.mark.parametrize("name", ACCEPTED)
def test_a_checkout_cloned_as_app_never_uses_the_parent_launcher(tmp_path: Path, name: str) -> None:
    root = _foreign_parent(tmp_path, name)
    assert pin.launcher_locations(root) == (_checkout_pair(root),)
    assert pin.verified_launcher(root) is None
    folder, pin_path = _checkout_pair(root)
    _populate(folder, pin_path, FILES)
    assert pin.verified_launcher(root) == folder / "VoolConfine.exe"


def test_a_marker_above_or_beside_the_app_folder_proves_nothing(tmp_path: Path) -> None:
    root = _foreign_parent(tmp_path, "app")
    (tmp_path / pin.INSTALL_MARKER_NAME).write_text("x", encoding="ascii")
    (tmp_path / "config" / pin.INSTALL_MARKER_NAME).write_text("x", encoding="ascii")
    assert pin.launcher_locations(root) == (_checkout_pair(root),)


def test_a_marker_that_is_a_folder_or_a_link_proves_nothing(tmp_path: Path) -> None:
    root = _foreign_parent(tmp_path, "app")
    pin.install_marker(root).mkdir()
    assert not pin.is_installed_app_root(root)
    pin.install_marker(root).rmdir()
    elsewhere = tmp_path / "planted-marker"
    elsewhere.write_text("x", encoding="ascii")
    try:
        pin.install_marker(root).symlink_to(elsewhere)
    except OSError:
        pytest.skip("this runner cannot make a symbolic link")   # the folder row above still runs
    assert not pin.is_installed_app_root(root)
    assert pin.launcher_locations(root) == (_checkout_pair(root),)


def test_a_relative_app_root_with_a_marker_still_never_probes_its_parent(tmp_path: Path, monkeypatch) -> None:
    _foreign_parent(tmp_path, "app")
    _mark_installed(tmp_path / "app")
    monkeypatch.chdir(tmp_path)
    assert pin.launcher_locations(Path("app")) == (_checkout_pair(Path("app")),)


# --- the follow-up install marker lives inside {app}\app, never beside or above it ---------------

@pytest.mark.parametrize("name", ACCEPTED + REFUSED)
def test_the_install_marker_resolves_inside_the_app_folder(tmp_path: Path, name: str) -> None:
    tmp_path = _base(tmp_path, name)
    root = tmp_path / "VOOL" / name
    root.mkdir(parents=True)
    marker = pin.install_marker(root)
    assert marker.parent == root
    assert marker.resolve().parent == root.resolve()
    assert root.resolve() in marker.resolve().parents
    assert tmp_path / "VOOL" not in (marker.parent, marker)
    assert marker.name == pin.INSTALL_MARKER_NAME


def test_the_install_marker_name_cannot_step_out_of_the_app_folder() -> None:
    name = pin.INSTALL_MARKER_NAME
    assert name and name not in (".", "..")
    assert not any(sep in name for sep in ("/", "\\", ":"))
    assert name[-1] not in ". "  # Windows would drop a trailing dot or space and alias another name
