from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by compatibility CI on Python 3.10
    import tomli as tomllib

from ops.pytest_shards import validate_pytest_args

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_GATE_COMMAND = (
    "python",
    "ops/verify.py",
    "--workers",
    "4",
    "--pytest-arg=--tb=short",
    "--log-dir=.verification-logs",
    "--tail-lines=2000",
    "--timeout-seconds",
    "7200",
)


def _load_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _named_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _assert_authoritative_contract(workflow: dict[str, Any]) -> None:
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    for event in ("push", "pull_request"):
        assert event in triggers
        assert "paths-ignore" not in triggers[event]
    verify = workflow["jobs"]["verify"]
    assert "if" not in verify

    setup_steps = [
        step for step in verify["steps"] if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert len(setup_steps) == 1
    assert setup_steps[0]["with"]["python-version"] == "3.12.13"

    gate_step = _named_step(verify, "Run authoritative verification")
    assert "if" not in gate_step
    command = tuple(shlex.split(gate_step["run"]))
    assert command == EXPECTED_GATE_COMMAND
    forwarded = tuple(
        token.removeprefix("--pytest-arg=")
        for token in command
        if token.startswith("--pytest-arg=")
    )
    validate_pytest_args(forwarded)

    upload = _named_step(verify, "Upload complete verification logs")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".verification-logs/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    assert workflow["jobs"]["build"]["needs"] == ["verify"]

    for job in workflow["jobs"].values():
        assert not job.get("continue-on-error", False)
        for step in job.get("steps", []):
            assert not step.get("continue-on-error", False)
            command_text = str(step.get("run", ""))
            assert "|| true" not in command_text
            assert "| tail" not in command_text
            assert "| tee" not in command_text


def test_push_and_pr_ci_use_the_exact_authoritative_gate() -> None:
    _assert_authoritative_contract(_load_workflow())


@pytest.mark.parametrize(
    "mutation",
    ("floating_python", "collect_only", "step_disabled", "job_disabled"),
)
def test_contract_mutations_are_rejected(mutation: str) -> None:
    workflow = copy.deepcopy(_load_workflow())
    verify = workflow["jobs"]["verify"]
    if mutation == "floating_python":
        setup = next(step for step in verify["steps"] if "setup-python" in step.get("uses", ""))
        setup["with"]["python-version"] = "3.12"
    elif mutation == "collect_only":
        gate = _named_step(verify, "Run authoritative verification")
        gate["run"] += " --pytest-arg=--collect-only"
    elif mutation == "step_disabled":
        _named_step(verify, "Run authoritative verification")["if"] = False
    else:
        verify["if"] = False

    with pytest.raises((AssertionError, ValueError)):
        _assert_authoritative_contract(workflow)


def test_verification_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = set(project["project"]["optional-dependencies"]["dev"])

    assert "pytest==9.1.0" in dev
    assert "ruff==0.16.7" in dev


def test_compatibility_keeps_complete_evidence_and_authoritative_deadline() -> None:
    job = _load_workflow()["jobs"]["compatibility"]
    run = _named_step(job, "Run compatibility collection and shards")["run"]
    assert "--timeout-seconds 7200" in run
    assert "--artifact-root=.compatibility-logs" in run
    upload = _named_step(job, "Upload complete compatibility logs")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".compatibility-logs/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"


def test_llm_lanes_install_shared_fixture_and_browser_dependencies() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/llm_acceptance.yml").read_text(encoding="utf-8")
    )
    for job in workflow["jobs"].values():
        install = _named_step(job, "Install dependencies")["run"]
        assert '.[dev,browser,companion]' in install
        browser = _named_step(job, "Provision browser test dependencies")
        assert browser["run"] == "python -m playwright install --with-deps chromium"


def test_security_audits_both_run_and_preserve_failure_evidence() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/security-audit.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["audit"]
    first = _named_step(job, "pip-audit (requirements)")
    second = _named_step(job, "pip-audit (runtime requirements)")
    assert "-r requirements.txt" in first["run"]
    assert "-r requirements-runtime.txt" in second["run"]
    assert second["if"] == "always()"
    for audit in (first, second):
        assert not audit.get("continue-on-error", False)
        assert "||" not in audit["run"]
        assert "--format=json -o .security-audit/" in audit["run"]
    upload = _named_step(job, "Upload Python audit evidence")
    assert upload["if"] == "always()"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"


def test_native_broker_transcript_preserves_exit_codes_and_is_uploaded_on_failure() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/windows-release-gauntlet.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["gauntlet"]
    run = _named_step(job, "Build and exercise native execution broker")["run"]
    assert "Start-Transcript -Path .windows-broker-logs/native-broker.log" in run
    assert "} finally {\n  Stop-Transcript" in run
    assert "& pwsh -NoLogo -NoProfile -NonInteractive -File ops/windows_native_ci_user.ps1" in run
    assert "$brokerExit = $LASTEXITCODE\n  if ($brokerExit -ne 0) { exit $brokerExit }" in run
    assert "continue-on-error" not in run
    upload = _named_step(job, "Upload native broker evidence")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".windows-broker-logs/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"


def test_native_standard_user_route_keeps_security_and_capture_guards() -> None:
    source = (REPO_ROOT / "ops/windows_native_ci_user.ps1").read_text(encoding="utf-8")
    for required in (
        "Assert-HostedRunner # Before any account", "RUNNER_ENVIRONMENT -ne 'github-hosted'",
        "New-LocalUser -Name $name -Password $secret", "'S-1-5-32-545'",
        "Users-only membership refused", "Marshal.ZeroFreeGlobalAllocUnicode(secret)",
        "const uint ProfileProcessCreationFlags = 0x4",
        "const uint ChildTokenAccess = 0xA",
        "OpenProcessToken(info.process, ChildTokenAccess, out token)",
        "CreateProcessWithLogonW(username, \".\", secret, 1, image, command, ProfileProcessCreationFlags",
        "Startup start=new Startup(); start.cb=(uint)Marshal.SizeOf(typeof(Startup))",
        "start.flags=1; start.show=0",
        "IntPtr.Zero, root, ref start, out info)",
        "AssignProcessToJobObject(job, info.process)", "uint previous=ResumeThread(info.thread)",
        "WaitForSingleObject(info.process, 600000)", "GetProcessTimes(info.process",
        "Marshal.WriteInt32(limits, 16, 0x2000)", "ConfigureOwnedJob(job)",
        "TerminateJobObject(job, 0xC000013A)", "WaitForSingleObject(info.process, 10000)",
        "if (!EmptyJob(job))", "Assert-NativeCompletion $native",
        "$launchAttempted = $true", "$null -ne $native -and $native.CleanupObserved",
        "Assert-AccountCleanup $observed $account.SID.Value $current", "Remove-LocalUser -SID $account.SID",
        "Assert-UserContext $context $ExpectedSid $binding",
        "& $tests $broker 2>&1 | Tee-Object", "$code = $LASTEXITCODE",
        "child-result.json", "input-manifest.json", "standard-user-receipt.json",
    ):
        assert required in source
    for prohibited in (
        "LOGON_NETCREDENTIALS_ONLY", "Set-LocalUser", "Set-ExecutionPolicy",
        "SetSecurityInfo", "SetUserObjectSecurity", "GetProcessById", "GetProcessesByName",
        "ConvertFrom-SecureString", "NetworkCredential", "PasswordInClearText",
    ):
        assert prohibited not in source
    assert source.count("[VoolCiEntry]::Run(") == 1
    assert source.count("Require(CreateProcessWithLogonW(") == 1
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/windows-release-gauntlet.yml").read_text(encoding="utf-8")
    )
    triggers = workflow.get("on", workflow.get(True))
    for event in ("push", "pull_request"):
        assert "ops/windows_native_ci_user.ps1" in triggers[event]["paths"]


def test_ci_child_admission_stages_precede_checks_and_single_resume() -> None:
    source = (REPO_ROOT / "ops/windows_native_ci_user.ps1").read_text(encoding="utf-8")
    checkpoints = (
        ('creation-identity', 'Require(GetProcessTimes(info.process'),
        ('child-token', 'Require(OpenProcessToken(info.process, ChildTokenAccess, out token)'),
        ('child-session', 'Require(GetTokenInformation(token, 12,'),
        ('child-identity', 'using (WindowsIdentity identity=new WindowsIdentity(token))'),
        ('child-user', 'if (identity.User.Value!=sid)'),
        ('child-owner', 'if (identity.Owner.Value!=sid)'),
        ('child-admin-role', 'if (new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))'),
        ('child-integrity', 'if (Integrity(token)!="S-1-16-8192")'),
        ('assign-job', 'Require(AssignProcessToJobObject(job, info.process)'),
        ('resume', 'uint previous=ResumeThread(info.thread)'),
    )
    previous = source.index('result.Stage="create-profile-process"')
    for stage, operation in checkpoints:
        current = source.index(f'result.Stage="{stage}";', previous)
        checked = source.index(operation, current)
        assert previous < current < checked
        previous = checked
    assert source.count('throw new InvalidOperationException("Created child token refused.")') == 4
    assert 'if (result.Session!=parent.SessionId)' in source
    assert 'OpenProcessToken(info.process, 8, out token)' not in source


def _run_entry_fixture(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    assert pwsh, "Windows CI requires real PowerShell contract fixtures"
    entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
    return subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         f". '{entry}'; {script}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap guards")
def test_ci_entry_refuses_local_host_before_account_operations(tmp_path: Path) -> None:
    result = _run_entry_fixture("$env:GITHUB_ACTIONS = ''; Invoke-HostedSuite", tmp_path)
    assert result.returncode != 0
    assert "account operations refused" in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows native interop compilation only")
def test_ci_entry_native_code_compiles_without_account_or_logon(tmp_path: Path) -> None:
    result = _run_entry_fixture(
        "Initialize-NativeEntry; $identity = [Security.Principal.WindowsIdentity]::GetCurrent(); "
        "try { [VoolCiEntry]::Integrity($identity.Token) } finally { $identity.Dispose() }", tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "S-1-16-" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Fixed Windows PowerShell child ABI")
def test_ci_entry_compiles_on_the_fixed_framework_powershell(tmp_path: Path) -> None:
    entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         f". '{entry}'; Initialize-NativeEntry; 'compiled'"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "compiled" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Real Windows empty-job setup without account or logon")
@pytest.mark.parametrize("framework", (False, True))
def test_ci_job_setup_observes_exact_kill_on_close_policy(tmp_path: Path, framework: bool) -> None:
    script = "Initialize-NativeEntry; [VoolCiEntry]::ProbeEmptyJobPolicy()"
    if framework:
        entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f". '{entry}'; {script}"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
        )
    else:
        result = _run_entry_fixture(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8192"


@pytest.mark.skipif(os.name != "nt", reason="Windows x64 profile-launch ABI without account or logon")
@pytest.mark.parametrize("framework", (False, True))
def test_ci_profile_launch_flags_and_abi_without_logon(tmp_path: Path, framework: bool) -> None:
    script = (
        "Initialize-NativeEntry; $entry = [VoolCiEntry]; "
        "$startup = $entry.GetNestedType('Startup', [Reflection.BindingFlags]::NonPublic); "
        "$process = $entry.GetNestedType('ProcessInfo', [Reflection.BindingFlags]::NonPublic); "
        "$sizeOf = [Runtime.InteropServices.Marshal].GetMethod('SizeOf', [type[]]@([type])); "
        "@{ creationFlags = $entry.GetField('ProfileProcessCreationFlags', "
        "[Reflection.BindingFlags]'NonPublic,Static').GetRawConstantValue(); "
        "startupBytes = $sizeOf.Invoke($null, @($startup)); "
        "processBytes = $sizeOf.Invoke($null, @($process)); "
        "flagsOffset = [Runtime.InteropServices.Marshal]::OffsetOf($startup, 'flags').ToInt32(); "
        "stdoutOffset = [Runtime.InteropServices.Marshal]::OffsetOf($startup, 'stdout').ToInt32() "
        "} | ConvertTo-Json -Compress"
    )
    if framework:
        entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f". '{entry}'; {script}"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
        )
    else:
        result = _run_entry_fixture(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "creationFlags": 4, "startupBytes": 104, "processBytes": 24,
        "flagsOffset": 60, "stdoutOffset": 88,
    }


@pytest.mark.skipif(os.name != "nt", reason="Real current-process token inspection without account or logon")
@pytest.mark.parametrize("framework", (False, True))
@pytest.mark.parametrize("duplicate", (False, True))
def test_ci_primary_token_role_access_and_cleanup(
    tmp_path: Path, framework: bool, duplicate: bool,
) -> None:
    access = (
        "[VoolCiEntry].GetField('ChildTokenAccess', "
        "[Reflection.BindingFlags]'NonPublic,Static').GetRawConstantValue()"
        if duplicate else "8"
    )
    script = r"""
Initialize-NativeEntry
$access = [uint32](ACCESS_VALUE)
$binding = [Reflection.BindingFlags]'NonPublic,Static'
$open = [VoolCiEntry].GetMethod('OpenProcessToken', $binding)
$close = [VoolCiEntry].GetMethod('CloseHandle', $binding)
$process = [Diagnostics.Process]::GetCurrentProcess()
$baseline = $null; $identity = $null; $managedToken = $null
$token = [IntPtr]::Zero; $closed = $false; $managedClosed = $false
$role = $null; $errorType = $null; $stage = 'open-token'
try {
    $threadIdentity = [Security.Principal.WindowsIdentity]::GetCurrent($true)
    if ($null -ne $threadIdentity) {
        $threadIdentity.Dispose(); throw 'Impersonated fixture context refused'
    }
    $baseline = [Security.Principal.WindowsIdentity]::GetCurrent()
    $expectedRole = ([Security.Principal.WindowsPrincipal]::new($baseline)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    $arguments = [object[]]@($process.Handle, $access, [IntPtr]::Zero)
    if (-not $open.Invoke($null, $arguments)) { throw 'Token open refused' }
    $token = [IntPtr]$arguments[2]
    $stage = 'identity'
    $identity = [Security.Principal.WindowsIdentity]::new($token)
    $managedToken = $identity.AccessToken
    $identityMatches = $identity.User.Value -eq $baseline.User.Value -and
        $identity.Owner.Value -eq $baseline.Owner.Value
    $primary = $identity.ImpersonationLevel.ToString() -eq 'None'
    $stage = 'admin-role'
    try {
        $role = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        $errorType = $_.Exception.GetBaseException().GetType().FullName
    }
} finally {
    try {
        if ($null -ne $identity) {
            $identity.Dispose(); $managedClosed = $managedToken.IsClosed
        }
    } finally {
        try {
            if ($token -ne [IntPtr]::Zero) {
                $closed = $close.Invoke($null, [object[]]@($token))
                if (-not $closed) { throw 'Original token close refused' }
            }
        } finally {
            if ($null -ne $baseline) { $baseline.Dispose() }
            $process.Dispose()
        }
    }
}
$threadIdentity = [Security.Principal.WindowsIdentity]::GetCurrent($true)
$noImpersonation = $null -eq $threadIdentity
if ($null -ne $threadIdentity) { $threadIdentity.Dispose() }
@{ access=$access; stage=$stage; errorType=$errorType; role=$role;
   expectedRole=$expectedRole; identityMatches=$identityMatches; primary=$primary;
   noImpersonation=$noImpersonation; originalClosed=$closed; managedClosed=$managedClosed
} | ConvertTo-Json -Compress
""".replace("ACCESS_VALUE", access)
    if framework:
        entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f". '{entry}'; {script}"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
        )
    else:
        result = _run_entry_fixture(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    observed = json.loads(result.stdout)
    assert observed["access"] == (10 if duplicate else 8)
    assert observed["stage"] == "admin-role"
    for fact in ("identityMatches", "primary", "noImpersonation", "originalClosed", "managedClosed"):
        assert observed[fact] is True, observed
    assert isinstance(observed["expectedRole"], bool)
    if duplicate:
        assert observed["errorType"] is None, observed
        assert observed["role"] == observed["expectedRole"]
    else:
        assert observed["errorType"] == "System.Security.SecurityException", observed
        assert observed["role"] is None


@pytest.mark.skipif(os.name != "nt", reason="Windows private test credential generation")
def test_ci_generated_secret_stays_secure_and_omitted_from_output(tmp_path: Path) -> None:
    result = _run_entry_fixture(
        "$secret = New-TestCredential; try { "
        "if ($secret -isnot [Security.SecureString] -or $secret.Length -ne 52 -or "
        "-not $secret.IsReadOnly()) { throw 'Secret representation refused' }; "
        "@{stage='test'; credentialLogged=$false} | ConvertTo-Json -Compress "
        "} finally { $secret.Dispose() }", tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"stage": "test", "credentialLogged": False}
    assert result.stderr == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap guards")
@pytest.mark.parametrize("fault", ("missing", "changed", "extra", "path_escape", "none"))
def test_ci_payload_guards_reject_missing_changed_and_extra_inputs(tmp_path: Path, fault: str) -> None:
    payload = tmp_path / "payload" / "tests"
    payload.mkdir(parents=True)
    (payload / "fixture.txt").write_text("reviewed", encoding="utf-8")
    script = (
        "$manifest = @(@{path='tests/fixture.txt'; "
        "sha256=(Get-FileHash payload/tests/fixture.txt).Hash}); "
    )
    script += {
        "missing": "Remove-Item -LiteralPath payload/tests/fixture.txt; ",
        "changed": "Set-Content -LiteralPath payload/tests/fixture.txt -Value changed; ",
        "extra": "New-Item -ItemType File -Path payload/tests/extra.txt | Out-Null; ",
        "path_escape": "$manifest[0].path = 'tests/../fixture.txt'; ",
        "none": "",
    }[fault]
    result = _run_entry_fixture(script + "Assert-InputManifest payload $manifest", tmp_path)
    assert (result.returncode == 0) == (fault == "none"), result.stderr


def test_ci_child_manifest_stages_bind_actual_loader_and_both_guard_calls() -> None:
    source = (REPO_ROOT / "ops/windows_native_ci_user.ps1").read_text(encoding="utf-8")
    child = source.split("function Invoke-ChildSuite {", 1)[1].split("function Invoke-HostedSuite {", 1)[0]
    loader = "$manifest = @(Get-Content -LiteralPath (Join-Path $Root 'input-manifest.json') -Raw | ConvertFrom-Json | ForEach-Object { $_ })"
    guard = "Assert-InputManifest (Join-Path $Root 'payload') $manifest"
    assert child.count(loader) == 1
    assert child.count(guard) == 2
    precheck = child.index(guard)
    native = child.index("$report.stage = 'native-suite'")
    postcheck = child.index(guard, precheck + len(guard))
    assert (
        child.index("$report.stage = 'load-input-manifest'")
        < child.index(loader)
        < child.index("$report.stage = 'verify-input-manifest'")
        < precheck < native
        < child.index("$report.exitCode = $code")
        < child.index("$report.stage = 'verify-final-input-manifest'")
        < postcheck < child.index("$report.stage = 'completed'")
    )


@pytest.mark.skipif(os.name != "nt", reason="Real child JSON loading and payload guards in both Windows shells")
@pytest.mark.parametrize("framework", (False, True))
@pytest.mark.parametrize("entry_count", (1, 201))
@pytest.mark.parametrize("fault", ("missing", "changed", "extra", "path_escape", "none"))
def test_ci_child_json_manifest_roundtrip_preserves_guards(
    tmp_path: Path, framework: bool, entry_count: int, fault: str,
) -> None:
    payload = tmp_path / "payload" / "tests"
    payload.mkdir(parents=True)
    manifest = []
    for index in range(entry_count):
        content = f"reviewed fixture {index}"
        name = f"fixture{index}.txt"
        (payload / name).write_text(content, encoding="utf-8")
        manifest.append({"path": f"tests/{name}", "sha256": hashlib.sha256(content.encode()).hexdigest()})
    if fault == "missing":
        (payload / "fixture0.txt").unlink()
    elif fault == "changed":
        (payload / "fixture0.txt").write_text("changed", encoding="utf-8")
    elif fault == "extra":
        (payload / "extra.txt").write_text("extra", encoding="utf-8")
    elif fault == "path_escape":
        manifest[0]["path"] = "tests/../fixture0.txt"
    (tmp_path / "input-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    source = (REPO_ROOT / "ops/windows_native_ci_user.ps1").read_text(encoding="utf-8")
    loaders = [line.strip() for line in source.splitlines() if line.strip().startswith("$manifest = @(Get-Content")]
    assert len(loaders) == 1
    # Execute the actual child statement, not an independently implemented test parser.
    script = (
        "$Root = (Get-Location).Path; " + loaders[0] + "; "
        "Assert-InputManifest (Join-Path $Root 'payload') $manifest; "
        f"if ($manifest.Count -ne {entry_count}) {{ throw 'Manifest shape differs' }}; 'verified'"
    )
    if framework:
        entry = REPO_ROOT / "ops/windows_native_ci_user.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Python inherits Core's module path; the profile-loaded child does not.
        environment = os.environ.copy()
        environment["PSMODULEPATH"] = str(powershell.parent / "Modules")
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f". '{entry}'; {script}"],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
    else:
        result = _run_entry_fixture(script, tmp_path)
    if fault == "none":
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "verified"
        assert result.stderr == ""
    else:
        assert result.returncode != 0
        expected = {
            "missing": "Payload inventory differs.",
            "extra": "Payload inventory differs.",
            "changed": "Payload hash or regular-file check failed:",
            "path_escape": "Unexpected payload path.",
        }[fault]
        assert expected in result.stderr, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap guards")
@pytest.mark.parametrize("fault", ("user", "owner", "admin", "integrity", "impersonation", "profile", "localAppData", "temp", "tmp", "none"))
def test_ci_context_guard_preserves_standard_user_profile_boundary(tmp_path: Path, fault: str) -> None:
    script = (
        "$context = @{ user='S-1-5-21-1-1001'; owner='S-1-5-21-1-1001'; admin=$false; "
        "integrity='S-1-16-8192'; impersonation='None'; profile='C:\\Users\\test'; "
        "localAppData='C:\\Users\\test\\AppData\\Local'; "
        "temp='C:\\Users\\test\\AppData\\Local\\Temp\\owned'; "
        "tmp='C:\\Users\\test\\AppData\\Local\\Temp\\owned' }; "
    )
    if fault == "admin":
        script += "$context.admin=$true; "
    elif fault != "none":
        script += f"$context.{fault}='wrong'; "
    result = _run_entry_fixture(
        script + "Assert-UserContext $context 'S-1-5-21-1-1001' 'C:\\Users\\test'", tmp_path,
    )
    assert (result.returncode == 0) == (fault == "none"), result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap guards")
@pytest.mark.parametrize("fault", ("incomplete", "timeout", "cleanup", "exit_unknown", "nonzero", "none"))
def test_ci_completion_guard_rejects_unknown_timeout_and_cleanup(tmp_path: Path, fault: str) -> None:
    script = "$native = @{Completed=$true; TimedOut=$false; CleanupObserved=$true; ExitCode=0}; "
    script += {
        "incomplete": "$native.Completed=$false; ", "timeout": "$native.TimedOut=$true; ",
        "cleanup": "$native.CleanupObserved=$false; ", "exit_unknown": "$native.ExitCode=$null; ",
        "nonzero": "$native.ExitCode=7; ", "none": "",
    }[fault]
    result = _run_entry_fixture(script + "Assert-NativeCompletion $native; exit $native.ExitCode", tmp_path)
    assert result.returncode == (0 if fault == "none" else 7 if fault == "nonzero" else 1), result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell bootstrap guards")
@pytest.mark.parametrize("fault", ("unknown_cleanup", "wrong_sid", "missing_account", "none"))
def test_ci_account_cleanup_guard_never_adopts_another_account(tmp_path: Path, fault: str) -> None:
    script = "$observed=$true; $account=@{SID=@{Value='created'}}; "
    script += {
        "unknown_cleanup": "$observed=$false; ", "wrong_sid": "$account.SID.Value='other'; ",
        "missing_account": "$account=$null; ", "none": "",
    }[fault]
    result = _run_entry_fixture(script + "Assert-AccountCleanup $observed 'created' $account", tmp_path)
    assert (result.returncode == 0) == (fault == "none"), result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows native PowerShell capture")
@pytest.mark.parametrize("framework", (False, True))
def test_native_output_capture_retains_stdout_stderr_and_nonzero_exit(tmp_path: Path, framework: bool) -> None:
    pwsh = (
        str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        if framework else shutil.which("pwsh")
    )
    assert pwsh, "Windows CI requires PowerShell to exercise native capture"
    script = (
        "$ErrorActionPreference = 'Stop'; Start-Transcript -Path transcript.log; try { "
        "$nativePreference = $ErrorActionPreference; try { $ErrorActionPreference = 'Continue'; "
        '& cmd /c "echo broker-stdout & echo broker-stderr 1>&2 & echo after-stderr & exit 7" '
        "2>&1 | Tee-Object -FilePath broker-output.log -ErrorAction Stop; "
        "$brokerExit = $LASTEXITCODE "
        "} finally { $ErrorActionPreference = $nativePreference }; "
        "if ($brokerExit -ne 0) { exit $brokerExit } "
        "} finally { Stop-Transcript }"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script], cwd=tmp_path,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 7
    output = (tmp_path / "broker-output.log").read_text(encoding="utf-16" if framework else "utf-8")
    assert "broker-stdout" in output and "broker-stderr" in output
    assert "after-stderr" in output
    assert "PowerShell transcript end" in (tmp_path / "transcript.log").read_text(encoding="utf-8")
