"""Compile the Windows installer contract without producing or installing an artifact."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "installer" / "bundle" / "vool.iss"


@pytest.mark.skipif(sys.platform != "win32", reason="Inno Setup runs on Windows")
def test_inno_setup_script_compiles_with_a_synthetic_stage(tmp_path: Path) -> None:
    compiler = shutil.which("ISCC.exe") or str(
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe"
    )
    if not Path(compiler).is_file():
        pytest.skip("Inno Setup compiler is unavailable")

    stage = tmp_path / "stage"
    for relative in (
        "app/stub.txt",
        "ollama/stub.txt",
            "python/stub.txt",
            "native/host/VOOL.exe",
            "native/broker/vool-broker.exe",
            "webview2/MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
        "bundle_manifest.json",
        "python_payload.json",
        "python-upgrade-bootstrap/python.exe",
        "python-upgrade-bootstrap/upgrade_python_payload.py",
            "bundle_supervisor.py",
            "migrate_install_state.py",
        "VOOL.cmd",
        "vool.vbs",
        "vool_window.py",
        "vool_protocol_handler.py",
    ):
        file = stage / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"synthetic compile fixture")

    result = subprocess.run(
        [
            compiler,
            "/O-",
            f"/DStage={stage}",
            f"/DOutDir={tmp_path}",
            "/DAppVersion=0.6.0-test",
            "/DAppPublisher=VOOL-Test",
            "/DBuildMode=Internal",
            "/DInstallerBaseName=VOOL-Setup-internal-0.6.0-test-deadbeef",
            str(SCRIPT),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.glob("VOOL-Setup*.exe"))
