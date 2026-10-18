"""The Windows installer ships the same lean runtime as the macOS bundle.

The first installed Windows smoke built, installed and launched a bundle whose API then refused to
serve: the served command surface imports zstandard at startup, and build_bundle.ps1 had never
installed it, while build_macos_app.sh always had. The same gap left PDF rendering (reportlab,
markdown-it-py) and the wallet's EVM signing and x402 SDK (eth-*, x402, web3) unusable on an
installed Windows build. Every package the macOS lean install names must be in the Windows one,
with the x402 SDK at the version pyproject pins and web3 at the lock's version.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import tomllib

from ops import bundle_dependency_provenance as prov

REPO = Path(__file__).resolve().parents[1]
WINDOWS_BUILD = REPO / "installer" / "bundle" / "build_bundle.ps1"


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _windows_lean_specifiers() -> list[str]:
    script = WINDOWS_BUILD.read_text(encoding="utf-8")
    marker = "& $py -m pip install --no-build-isolation"
    assert script.count(marker) == 1, "the Windows lean dependency install is no longer one pip call"
    body = script.split(marker, 1)[1].split("if ($LASTEXITCODE", 1)[0]
    body = body.replace("`\r\n", " ").replace("`\n", " ")
    return [token for token in shlex.split(body) if not token.startswith("-")]


def _windows_lean_names() -> set[str]:
    return {_normalized(re.split(r"[<>=!~\[]", spec, maxsplit=1)[0]) for spec in _windows_lean_specifiers()}


def _locked(name: str) -> str:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    return next(package["version"] for package in lock["package"] if package["name"] == name)


def test_the_windows_lean_install_is_read_as_packages_not_flags():
    names = _windows_lean_names()
    assert {"pydantic", "cryptography", "pywebview", "pythonnet"} <= names
    for phantom in ("q", "no-build-isolation", "no-warn-script-location", "py", "if"):
        assert phantom not in names, f"{phantom!r} read as a package"


def test_every_macos_lean_package_is_installed_by_the_windows_bundle():
    missing = sorted({_normalized(name) for name in prov.lean_requirements()} - _windows_lean_names())
    assert not missing, f"the Windows bundle drops packages the macOS bundle ships: {missing}"


def test_the_served_startup_import_is_in_the_windows_bundle():
    assert "zstandard" in _windows_lean_names(), "without zstandard the installed API refuses to serve"


def test_the_windows_bundle_ships_the_x402_sdk_at_the_pinned_and_locked_versions():
    extra = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["x402"]
    assert len(extra) == 1, extra
    specifiers = _windows_lean_specifiers()
    assert extra[0] in specifiers, "the Windows bundle dropped the x402 SDK: the Base x402 channel would be unusable in the app"
    assert f"web3=={_locked('web3')}" in specifiers, "the Windows bundle's web3 left the lock's version"
