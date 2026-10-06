"""Compile/assemble only the small installer verifier in disposable test state."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh")
EMBEDDED = os.environ.get("VOOL_TEST_EMBEDDED_PYTHON", "")


@pytest.mark.skipif(not POWERSHELL or not EMBEDDED, reason="Explicit existing embedded Python test input required")
def test_actual_builder_inventory_and_site_free_bootstrap(tmp_path):
    source = Path(EMBEDDED)
    assert source.is_file()
    stage = tmp_path / "Programs/VOOL"
    python = stage / "python"
    python.mkdir(parents=True)
    # Copy only existing interpreter/stdlib bytes, not a runtime, model store or profile.
    for item in source.parent.iterdir():
        if item.is_file():
            shutil.copy2(item, python / item.name)
    poison = python / "Lib/site-packages/sitecustomize.py"
    poison.parent.mkdir(parents=True)
    marker = tmp_path / "site-executed"
    poison.write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')", encoding="utf-8")
    (stage / "bundle_manifest.json").write_text(json.dumps({
        "schema": "vool.bundle_manifest.v1", "source_commit_full": "1" * 40,
    }), encoding="utf-8")
    env = {**os.environ, "UPGRADE_BUILDER": str(ROOT / "installer/bundle/build_bundle.ps1"),
           "UPGRADE_STAGE": str(stage), "UPGRADE_PYTHON": str(python / "python.exe"),
           "UPGRADE_HELPER": str(ROOT / "installer/bundle/upgrade_python_payload.py")}
    script = r"""
    $ErrorActionPreference = 'Stop'
    $tokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $env:UPGRADE_BUILDER, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw $parseErrors[0] }
    $function = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Write-PythonUpgradeInputs'
    }, $false)
    if (-not $function) { throw 'Upgrade input helper missing' }
    . ([scriptblock]::Create($function.Extent.Text))
    Write-PythonUpgradeInputs -Stage $env:UPGRADE_STAGE `
        -PythonDir (Split-Path -Parent $env:UPGRADE_PYTHON) `
        -PythonExe $env:UPGRADE_PYTHON -Helper $env:UPGRADE_HELPER
    """
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((stage / "python_payload.json").read_text())
    assert "Lib/site-packages/sitecustomize.py" in manifest["files"]
    bootstrap = stage / "python-upgrade-bootstrap"
    assert not (bootstrap / "Lib").exists()
    path_file = next(bootstrap.glob("python*._pth"))
    assert "import site" not in path_file.read_text()
    assert "site-packages" not in path_file.read_text()
    result = subprocess.run([str(bootstrap / "python.exe"), "-I", "-S", "-B", "-c",
                             "import sys; print('site' in sys.modules)"],
                            env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    home = tmp_path / "profile/VOOL"
    before = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*"))
    result = subprocess.run([str(bootstrap / "python.exe"), "-I", "-S", "-B",
                             str(bootstrap / "upgrade_python_payload.py"), "check", "--root", str(stage),
                             "--programs", str(stage.parent), "--home", str(home), "--manifest",
                             str(stage / "python_payload.json")],
                            env=env, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert not home.exists()
    assert sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*")) == before
    assert not marker.exists()


def test_builder_inventory_is_after_final_cleanup_and_no_package_imports():
    builder = (ROOT / "installer/bundle/build_bundle.ps1").read_text()
    final = builder[builder.rindex("# Python bytecode caches"):]
    assert final.index('".pyc", ".pdb"') < final.index("Write-PythonUpgradeInputs -Stage")
    helper = (ROOT / "installer/bundle/upgrade_python_payload.py").read_text()
    assert "subprocess" not in helper
    assert "rmtree" not in helper and ".unlink(" not in helper
