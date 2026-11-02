"""The Windows bundle installs one pinned pip wheel, checked by sha256, never whatever get-pip serves today.

build_bundle.ps1 fetched the unpinned https://bootstrap.pypa.io/get-pip.py, so every build took the
newest pip. pip 26.3 (2 Nov 2026) refuses to run that way on the embeddable Python, and every fresh
installer build failed with "Embedded Python pip bootstrap failed." The build now downloads one
pinned pip wheel from files.pythonhosted.org, refuses it unless its sha256 is the pinned one, and
installs pip from that wheel with no index, so the pip in the bundle is the same on every build.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "installer" / "bundle" / "build_bundle.ps1"


def _source() -> str:
    return BUILD.read_text(encoding="utf-8")


def _pins(source: str) -> tuple[str, str, str]:
    version = re.search(r'^\$PipVersion = "([0-9]+(?:\.[0-9]+)+)"$', source, re.M)
    url = re.search(r'^\$PipWheelUrl = "(https://files\.pythonhosted\.org/packages/[^"]+)"$', source, re.M)
    sha = re.search(r'^\$PipWheelSha256 = "([0-9a-f]{64})"$', source, re.M)
    assert version and url and sha, "build_bundle.ps1 must pin $PipVersion, $PipWheelUrl and $PipWheelSha256"
    return version.group(1), url.group(1), sha.group(1)


def test_no_unpinned_pip_bootstrap_is_fetched() -> None:
    source = _source()

    assert "bootstrap.pypa.io" not in source
    assert "get-pip" not in source


def test_the_pinned_wheel_is_the_pinned_pip_version() -> None:
    version, url, _ = _pins(_source())

    assert url.endswith(f"/pip-{version}-py3-none-any.whl")


def test_the_wheel_is_refused_unless_its_sha256_is_the_pinned_one_before_it_runs() -> None:
    source = _source()
    download = source.index("-Uri $PipWheelUrl")
    check = source.index("Get-FileHash -Algorithm SHA256 -LiteralPath $pipWheel")
    refuse = source.index("throw \"Embedded Python pip wheel sha256 mismatch")
    run = source.index("& $py -c")

    assert download < check < refuse < run


def test_pip_is_installed_from_that_wheel_with_no_index() -> None:
    source = _source()
    run = source[source.index("& $py -c") :]
    run = run[: run.index("\n")]

    assert "--no-index" in run and "$pipWheel" in run
    assert "Embedded Python pip bootstrap failed." in source
