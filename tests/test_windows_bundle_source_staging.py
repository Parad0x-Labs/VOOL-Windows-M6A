"""Exercise Internal source staging without running downloads, builds or VOOL."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_BUILDER = Path(__file__).resolve().parents[1] / "installer/bundle/build_bundle.ps1"
_POWERSHELL = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(_POWERSHELL is None, reason="PowerShell 7 required")


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    for relative, content in {
        ".gitignore": "*.log\n",
        "core/identity.py": "original tracked bytes\n",
        "core/name with spaces-é.py": "literal tracked path\n",
        "installer/bundle/entry.py": "tracked installer source\n",
        "README.md": "grounding\n",
        "docs/SYSTEM_SPINE.md": "canonical grounding\n",
        "docs/not-shipped.md": "not runtime grounding\n",
    }.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=KAS-049", "-c", "user.email=fixture@invalid", "commit", "-m", "fixture")
    return root


def _stage(source, app, action=""):
    # Load only actual staging function ASTs. Never dot-source the executable builder.
    script = r"""
    $ErrorActionPreference = 'Stop'
    $tokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $env:STAGING_BUILDER, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw $parseErrors[0] }
    foreach ($name in @('Copy-InternalBundleSource', 'Assert-InternalBundlePayload')) {
        $function = $ast.Find({ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
        }, $false)
        if (-not $function) { throw "Staging function missing: $name" }
        . ([scriptblock]::Create($function.Extent.Text))
    }
    $files = @(Copy-InternalBundleSource -RepoRoot $env:STAGING_SOURCE `
        -AppDir $env:STAGING_APP -Paths @('core', 'installer', 'README.md', 'docs\SYSTEM_SPINE.md'))
    """ + action + r"""
    Assert-InternalBundlePayload -RepoRoot $env:STAGING_SOURCE `
        -AppDir $env:STAGING_APP -SourceFiles $files
    """
    env = {**os.environ, "STAGING_BUILDER": str(_BUILDER), "STAGING_SOURCE": str(source), "STAGING_APP": str(app)}
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_internal_stages_dirty_tracked_bytes_only(source, tmp_path):
    (source / "core/identity.py").write_bytes(b"approved dirty bytes\r\n")
    residue = source / "installer/bundle/VOOL/open.log"
    residue.parent.mkdir()
    residue.write_bytes(b"ignored residue must remain\n")
    (source / "core/untracked.py").write_text("must not ship", encoding="utf-8")
    before = _git(source, "status", "--porcelain", "--untracked-files=all").stdout
    app = tmp_path / "app"
    result = _stage(source, app)
    assert result.returncode == 0, result.stderr
    assert (app / "core/identity.py").read_bytes() == b"approved dirty bytes\r\n"
    assert {str(p.relative_to(app)).replace("\\", "/") for p in app.rglob("*") if p.is_file()} == {
        "core/identity.py", "core/name with spaces-é.py", "installer/bundle/entry.py",
        "README.md", "docs/SYSTEM_SPINE.md",
    }
    assert residue.read_bytes() == b"ignored residue must remain\n"
    assert _git(source, "status", "--porcelain", "--untracked-files=all").stdout == before


@pytest.mark.parametrize("action,error", [
    ("Set-Content -LiteralPath (Join-Path $env:STAGING_APP 'core/extra.log') 'unexpected'", "Unexpected app payload file"),
    ("Remove-Item -LiteralPath (Join-Path $env:STAGING_APP 'core/identity.py')", "Missing app payload file"),
    ("Set-Content -LiteralPath (Join-Path $env:STAGING_APP 'core/identity.py') 'changed'", "App payload differs from source"),
])
def test_payload_gate_refuses_unexpected_missing_or_changed_files(source, tmp_path, action, error):
    result = _stage(source, tmp_path / "app", action)
    assert result.returncode != 0
    assert error in result.stderr


def test_generated_source_stamp_is_the_only_exception(source, tmp_path):
    result = _stage(source, tmp_path / "app", r"""
        New-Item -ItemType Directory (Join-Path $env:STAGING_APP 'config') | Out-Null
        Set-Content -LiteralPath (Join-Path $env:STAGING_APP 'config/build-source.json') '{}'
    """)
    assert result.returncode == 0, result.stderr


def test_missing_tracked_source_is_not_silently_omitted(source, tmp_path):
    (source / "core/identity.py").unlink()
    result = _stage(source, tmp_path / "app")
    assert result.returncode != 0
    assert "Tracked bundle source missing" in result.stderr


def test_nonempty_app_destination_is_preserved_and_refused(source, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    canary = app / "preserve.txt"
    canary.write_bytes(b"never overwrite")
    result = _stage(source, app)
    assert result.returncode != 0
    assert "Internal app stage must be empty" in result.stderr
    assert canary.read_bytes() == b"never overwrite"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction fixture")
def test_tracked_source_junction_is_refused(source, tmp_path):
    original = (source / "core/identity.py").read_bytes()
    external = tmp_path / "outside"
    (source / "core").rename(external)
    env = {**os.environ, "STAGING_LINK": str(source / "core"), "STAGING_TARGET": str(external)}
    subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         "New-Item -ItemType Junction -Path $env:STAGING_LINK -Target $env:STAGING_TARGET | Out-Null"],
        env=env, check=True, capture_output=True, timeout=30,
    )
    result = _stage(source, tmp_path / "app")
    assert result.returncode != 0
    assert "Reparse point in tracked bundle source" in result.stderr
    assert (external / "identity.py").read_bytes() == original


def test_builder_keeps_release_archive_and_dirty_identity_contract():
    script = _BUILDER.read_text(encoding="utf-8")
    assert '& git -C $RepoRoot archive --format=zip --output=$releaseArchive $commitFull' in script
    assert 'if ($dirty) { throw "Release bundle source checkout is dirty." }' in script
    assert 'Bundle stage must be empty.' in script
    assert 'if ($BuildMode -eq "Internal")' in script
    assert '$internalSourceFiles = @(Copy-InternalBundleSource' in script
    assert script.count('Assert-InternalBundlePayload -RepoRoot $RepoRoot') == 2
    assert '--root $RepoRoot --out $buildSourcePath' in script
    assert 'source_dirty_state = [bool]$buildSource.dirty_state' in script
