"""Windows ships no system tz database, so the clock lane reads zone tables from the pip `tzdata`
package. That package only reached Windows installs transitively (through pandas), so a dependency
change upstream could silently empty the city-to-zone table again. It is declared directly, for
Windows only, in every place the runtime's dependencies are listed.
"""
from __future__ import annotations

import re
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = re.compile(r"""^tzdata\b[^;]*;\s*sys_platform\s*==\s*['"]win32['"]\s*$""")


def _requirement_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split("  #", 1)[0].strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_pyproject_declares_tzdata_for_windows_only() -> None:
    dependencies = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert [d for d in dependencies if WINDOWS_ONLY.match(d)], dependencies


def test_both_requirements_projections_declare_tzdata_for_windows_only() -> None:
    for name in ("requirements.txt", "requirements-runtime.txt"):
        lines = _requirement_lines(REPO / name)
        assert [line for line in lines if WINDOWS_ONLY.match(line)], name


def test_the_lock_names_tzdata_as_a_direct_windows_dependency() -> None:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    vool = next(package for package in lock["package"] if package["name"] == "vool")
    direct = [dep for dep in vool["dependencies"] if dep["name"] == "tzdata"]
    assert direct and "win32" in direct[0].get("marker", ""), direct


def test_the_windows_bundle_installs_tzdata() -> None:
    script = (REPO / "installer" / "bundle" / "build_bundle.ps1").read_text(encoding="utf-8")
    body = script.split("& $py -m pip install --no-build-isolation", 1)[1].split("if ($LASTEXITCODE", 1)[0]
    assert re.search(r"(?<![\w-])\"?tzdata\b", body), "the Windows bundle does not install tzdata"
