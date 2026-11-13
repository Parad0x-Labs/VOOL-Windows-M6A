"""The Windows bundle's embedded Python ships pytest, pinned by version and sha256.

The installed 0.7.1 bundle had 164 site-packages and no pytest, so "run pytest in <marked folder>"
always failed with "No module named pytest": the sandbox runs `pytest` and `python -m pytest` as
the bundled interpreter, and only unittest (stdlib) could run. build_bundle.ps1 now installs
installer/bundle/requirements-windows-pytest.txt with --require-hashes --no-deps, so every runtime
dependency of pytest on Windows with Python 3.12 must be in that file, at the version and wheel
hash uv.lock pins, and pytest must be the dev pin pyproject.toml declares.
"""
from __future__ import annotations

import re
from pathlib import Path

import tomllib
from packaging.markers import Marker

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "installer" / "bundle" / "build_bundle.ps1"
REQUIREMENTS = REPO / "installer" / "bundle" / "requirements-windows-pytest.txt"
EXPECTED = {"pytest", "pluggy", "iniconfig", "packaging", "pygments", "colorama"}


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, tuple[str, list[str]]]:
    text = REQUIREMENTS.read_text(encoding="utf-8")
    logical = re.sub(r"\\\r?\n", " ", text)
    pins: dict[str, tuple[str, list[str]]] = {}
    for line in logical.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.]*)((?:\s+--hash=sha256:[0-9a-f]{64})+)", line)
        assert match, f"not an exact, hash-pinned requirement: {line!r}"
        name = _normalized(match.group(1))
        assert name not in pins, f"{name} is listed twice"
        hashes = re.findall(r"sha256:([0-9a-f]{64})", match.group(3))
        pins[name] = (match.group(2), hashes)
    return pins


def _lock() -> dict[str, dict]:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    return {_normalized(package["name"]): package for package in lock["package"]}


def _marker_applies_to_windows_312(marker: str) -> bool:
    """True when a uv.lock dependency marker holds on win32 with the bundle's Python 3.12 and no
    extras (uv's extras-conflict clauses then evaluate false)."""
    environment = {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
                   "python_version": "3.12", "python_full_version": "3.12.7", "extra": ""}
    return Marker(marker).evaluate(environment)


def test_the_bundle_spec_pins_pytest_and_its_runtime_deps():
    assert set(_pins()) == EXPECTED


def test_pytest_is_the_dev_pin_the_gate_uses():
    dev = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    version, _ = _pins()["pytest"]
    assert f"pytest=={version}" in dev


def test_every_pin_is_the_wheel_uv_lock_pins():
    lock = _lock()
    for name, (version, hashes) in _pins().items():
        package = lock[name]
        assert package["version"] == version, f"{name}=={version} is not the lock's {package['version']}"
        wheels = {wheel["hash"].removeprefix("sha256:") for wheel in package.get("wheels", [])}
        assert hashes and set(hashes) <= wheels, f"{name}: hash is not a wheel uv.lock pins"


def test_no_runtime_dependency_of_pytest_on_windows_is_left_out():
    lock = _lock()
    pins = _pins()
    needed: set[str] = set()
    todo = ["pytest"]
    while todo:
        name = todo.pop()
        if name in needed:
            continue
        needed.add(name)
        for dependency in lock[name].get("dependencies", []):
            marker = dependency.get("marker")
            if marker is None or _marker_applies_to_windows_312(marker):
                todo.append(_normalized(dependency["name"]))
    assert needed == set(pins), f"--no-deps install would miss {sorted(needed - set(pins))}"


def test_the_build_installs_the_spec_by_hash_after_the_lean_deps_and_before_pip_check():
    script = BUILD.read_text(encoding="utf-8")
    assert '"requirements-windows-pytest.txt"' in script
    install = script.index("--require-hashes --no-deps --only-binary=:all:")
    line = script[script.rindex("\n", 0, install) : script.index("if ($LASTEXITCODE", install)]
    assert "& $py -m pip install" in line and "-r $PytestRequirements" in line
    lean = script.index("Embedded Python runtime dependency installation failed.")
    failed = script.index('throw "Embedded Python pytest installation failed."')
    starts = script.index("& $py -m pytest --version")
    check = script.index("& $py -m pip check")
    assert lean < install < failed < starts < check
    assert 'throw "Embedded Python pytest does not start."' in script
