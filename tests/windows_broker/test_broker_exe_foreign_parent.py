"""A checkout never starts a VoolBroker.exe planted in its parent folder (partial for E2-seams-08#1).

Same rule as the confinement pin: only an absolute app root named `app` probes `{app}\\broker`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.windows_broker.process import broker_executable

REFUSED = ["vool", "repo", "apps", "myapp", "app.old", "app ", "_app", "application", "VOOL"]
ACCEPTED = ["app", "App", "APP", "aPp"]


def _base(tmp_path: Path, name: str) -> Path:
    """Win32 drops a trailing space or dot from a folder name; only the \\\\?\\ spelling keeps it, so on
    Windows a row like "app " is built there to really test a folder of that name."""
    if os.name == "nt" and name[-1:] in (" ", "."):
        return Path("\\\\?\\" + str(tmp_path))
    return tmp_path


def _mark_installed(app: Path) -> None:
    from sandbox.windows_confinement_pin import install_marker

    install_marker(app).write_text("VOOL installed app folder", encoding="ascii")   # what the bundle build stages


def _plant_parent_broker(parent: Path) -> Path:
    exe = parent / "broker" / "VoolBroker.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ foreign broker")
    return exe


def _publish(root: Path) -> Path:
    exe = root / "windows" / "broker" / "publish" / "VoolBroker.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ checkout broker")
    return exe


@pytest.mark.parametrize("name", REFUSED)
def test_a_checkout_without_a_publish_never_gets_the_parent_broker(tmp_path: Path, name: str) -> None:
    tmp_path = _base(tmp_path, name)
    _plant_parent_broker(tmp_path)
    root = tmp_path / name
    root.mkdir()
    assert broker_executable(root) is None


@pytest.mark.parametrize("name", REFUSED)
def test_a_checkout_gets_its_own_publish_not_the_parent_broker(tmp_path: Path, name: str) -> None:
    tmp_path = _base(tmp_path, name)
    _plant_parent_broker(tmp_path)
    root = tmp_path / name
    published = _publish(root)
    assert broker_executable(root) == published


@pytest.mark.parametrize("relative", [Path("app"), Path("APP"), Path("vool")])
def test_a_relative_root_never_gets_the_parent_broker(tmp_path: Path, monkeypatch, relative: Path) -> None:
    # The parent of a relative root is the current folder, where a broker has been planted.
    _plant_parent_broker(tmp_path)
    (tmp_path / relative).mkdir()
    monkeypatch.chdir(tmp_path)
    assert broker_executable(relative) is None


@pytest.mark.parametrize("name", ACCEPTED)
def test_an_app_root_still_finds_the_installed_broker(tmp_path: Path, name: str) -> None:
    install = tmp_path / "VOOL"
    exe = _plant_parent_broker(install)
    app = install / name
    app.mkdir()
    _mark_installed(app)
    assert broker_executable(app) == exe


@pytest.mark.parametrize("name", ACCEPTED)
def test_a_checkout_cloned_as_app_never_gets_the_parent_broker(tmp_path: Path, name: str) -> None:
    """E2-seams-08#1: without the install marker inside it, an `app` folder is a checkout."""
    _plant_parent_broker(tmp_path)
    app = tmp_path / name
    app.mkdir()
    assert broker_executable(app) is None
    published = _publish(app)
    assert broker_executable(app) == published


@pytest.mark.parametrize("name", ACCEPTED)
def test_an_app_root_prefers_the_installed_broker_over_a_publish(tmp_path: Path, name: str) -> None:
    install = tmp_path / "VOOL"
    exe = _plant_parent_broker(install)
    app = install / name
    _publish(app)
    _mark_installed(app)
    assert broker_executable(app) == exe
