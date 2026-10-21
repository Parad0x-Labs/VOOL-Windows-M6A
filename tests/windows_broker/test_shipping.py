"""The installer ships the self-contained broker where the API looks for it (phase B)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_installer_puts_the_broker_beside_the_app() -> None:
    iss = _read("installer/bundle/vool.iss")
    entry = re.search(r'^Source: "\{#Stage\}\\broker\\\*"; DestDir: "([^"]+)"; Flags: ([^\r\n]+)$', iss, re.MULTILINE)
    assert entry, "vool.iss does not ship {#Stage}\\broker"
    # core.windows_broker.process.broker_executable looks in <app root>\broker, beside {app}\app.
    assert entry.group(1) == "{app}\\broker"
    flags = entry.group(2).split()
    assert "recursesubdirs" in flags and "uninsneveruninstall" not in flags


def test_the_bundle_publishes_the_broker_into_the_stage() -> None:
    script = _read("installer/bundle/build_bundle.ps1")
    assert '$brokerDir = Join-Path $Stage "broker"' in script
    assert '"windows\\broker\\publish.ps1") -Output $brokerDir' in script
    assert 'throw "Broker publish produced no VoolBroker.exe' in script


def test_the_publish_is_self_contained_and_pinned() -> None:
    script = _read("windows/broker/publish.ps1")
    for argument in ('"-r", "win-x64"', '"--self-contained"', '"-p:RuntimeFrameworkVersion=10.0.13"'):
        assert argument in script
    assert 'Join-Path $PSScriptRoot "publish"' in script
    # A checkout's API starts the broker from the default publish folder.
    from core.windows_broker.process import broker_executable

    assert broker_executable(ROOT) in (None, ROOT / "windows" / "broker" / "publish" / "VoolBroker.exe")


def test_the_publish_folder_is_not_tracked() -> None:
    assert "publish/" in _read("windows/broker/.gitignore").splitlines()
