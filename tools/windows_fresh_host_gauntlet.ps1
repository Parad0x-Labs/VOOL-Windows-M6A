[CmdletBinding()]
param(
    [ValidateSet("auto-recommended", "local-only", "local-max", "ollama-only", "ollama-max")]
    [string]$InstallProfile = "auto-recommended",
    [string]$VoolHome = "",
    [string]$PackageVersion = "",
    [int]$BenchmarkTimeoutSeconds = 240,
    [switch]$SkipInstall,
    [switch]$SkipBenchmark,
    [switch]$SkipPackageBuild,
    [switch]$RequireOpenClaw,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$StartedAt = Get-Date
$ReportDir = Join-Path $ProjectRoot "dist\windows-gauntlet"
$Checks = New-Object System.Collections.Generic.List[object]
New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null

function Add-Check {
    param(
        [string]$Name,
        [string]$Status,
        [Nullable[int]]$ExitCode,
        [double]$DurationSeconds,
        [string]$Detail
    )

    $Checks.Add([ordered]@{
        name = $Name
        status = $Status
        exitCode = $ExitCode
        durationSeconds = [Math]::Round($DurationSeconds, 2)
        detail = $Detail
    }) | Out-Null
}

function Resolve-Tool {
    param([string]$Name)

    $tools = @(Get-Command $Name -All -ErrorAction SilentlyContinue)
    if ($tools.Count -eq 0) {
        return ""
    }

    $selected = $tools |
        Where-Object { $_.Source -match "\.(cmd|exe)$" } |
        Select-Object -First 1
    if (-not $selected) {
        $selected = $tools | Select-Object -First 1
    }

    return [string]$selected.Source
}

function Resolve-Python {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $python = Resolve-Tool "python"
    if (-not [string]::IsNullOrWhiteSpace($python)) {
        return $python
    }

    $py = Resolve-Tool "py"
    if (-not [string]::IsNullOrWhiteSpace($py)) {
        return $py
    }

    throw "Python was not found. Install Python 3.10 or newer and rerun."
}

function Convert-ToSafeLogName {
    param([string]$Name)
    return (($Name -replace "[^A-Za-z0-9_.-]", "_").Trim("_"))
}

function Invoke-Checked {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [switch]$AllowSkip
    )

    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        if ($AllowSkip) {
            Add-Check -Name $Name -Status "skipped" -ExitCode $null -DurationSeconds 0 -Detail "Required command was not found."
            return
        }
        throw "Required command for '$Name' was not found."
    }

    $started = Get-Date
    $logName = (Convert-ToSafeLogName $Name) + "-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log"
    $logPath = Join-Path $ReportDir $logName
    Push-Location $ProjectRoot
    try {
        if (-not $Json) {
            Write-Host "==> $Name"
        }
        & $FilePath @Arguments *> $logPath
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    $duration = ((Get-Date) - $started).TotalSeconds
    $detail = "$FilePath $($Arguments -join ' ') | log=$logPath"
    Add-Check -Name $Name -Status ($(if ($exitCode -eq 0) { "passed" } else { "failed" })) -ExitCode $exitCode -DurationSeconds $duration -Detail $detail
    if ($exitCode -ne 0) {
        if (Test-Path -LiteralPath $logPath) {
            $tail = (Get-Content -LiteralPath $logPath -Tail 60 -ErrorAction SilentlyContinue) -join "`n"
            if (-not [string]::IsNullOrWhiteSpace($tail)) {
                Write-Host $tail
            }
        }
        throw "$Name failed with exit code $exitCode."
    }
}

function Invoke-Install {
    if ($SkipInstall) {
        Add-Check -Name "VOOL installer" -Status "skipped" -ExitCode $null -DurationSeconds 0 -Detail "Skipped by flag."
        return
    }

    $installer = Join-Path $ProjectRoot "Install_And_Run_VOOL.ps1"
    $args = @("-InstallProfile", $InstallProfile, "-AutoYes", "-NoStart")
    if (-not [string]::IsNullOrWhiteSpace($VoolHome)) {
        $args += @("-VoolHome", $VoolHome)
    }
    if ($SkipBenchmark) {
        $args += "-SkipBenchmark"
    }

    Invoke-Checked -Name "VOOL installer" -FilePath "powershell" -Arguments (@("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer) + $args)
}

function Invoke-FocusedRegression {
    $python = Resolve-Python
    $tests = @(
        "tests\test_hardware_tier.py",
        "tests\test_model_store_planner.py",
        "tests\test_provider_probe.py",
        "tests\test_provider_probe_contract.py",
        "tests\test_install_script_contract.py",
        "tests\test_install_surface_contracts.py",
        "tests\test_local_only_policy.py",
        "tests\test_validate_install_profile.py"
    )
    Invoke-Checked -Name "Focused Windows regression tests" -FilePath $python -Arguments (@("-m", "pytest") + $tests + @("-q", "--tb=short"))
}

function Invoke-WindowsRuntimeRegression {
    # The runtime itself on Windows: the API server and window start, the forget authority,
    # draft/credential locks, POSIX-only calls and the installer's and hard stop's process
    # ownership. Run in UTF-8 mode, as the installed app's launcher runs every bundled Python.
    $python = Resolve-Python
    $tests = @(
        "tests\test_windows_runtime_startup.py",
        "tests\test_windows_posix_only_calls.py",
        "tests\test_os_file_lock.py",
        "tests\test_windows_utf8_mode.py",
        "tests\test_windows_installer_ownership.py",
        "tests\test_windows_installer_stops_only_this_install.py",
        "tests\test_windows_installed_notes_live_in_the_vool_home.py",
        "tests\test_windows_installed_unbound_chat_uses_the_desktop.py",
        "tests\test_windows_stop_spares_foreign_listeners.py",
        "tests\test_process_liveness_never_kills.py",
        "tests\test_windows_mutation_ledger_names.py",
        "tests\test_windows_relaunch_reopens_a_closed_window.py",
        "tests\test_windows_junction_boundary.py",
        "tests\test_windows_write_gate.py",
        "tests\test_windows_write_gate_before_approval.py",
        "tests\test_machine_writes_are_checked_where_they_land.py",
        "tests\test_media_editor_export_asks_the_windows_gate.py",
        "tests\test_git_tools_repository_config_cannot_run_programs.py",
        "tests\test_safe_git_worktree_config_cannot_run_programs.py",
        "tests\test_safe_git_lfs_and_signing_by_command.py",
        "tests\repoops\test_local_lifecycle_checks_git_lfs_servers.py",
        "tests\repoops\test_local_lifecycle_refuses_share_paths.py",
        "tests\test_git_tools_diff_refuses_share_pathspecs.py",
        "tests\test_user_rules_reused_message_is_not_an_option.py",
        "tests\test_windows_gate_checks_where_git_writes.py",
        "tests\test_windows_gate_refuses_git_writes_sent_elsewhere.py",
        "tests\test_git_follows_links_in_its_git_folder.py",
        "tests\repoops\test_windows_push_lands_only_in_marked_folders.py",
        "tests\test_windows_gate_checks_the_task_and_session_folder.py",
        "tests\test_windows_patch_lands_only_where_checked.py",
        "tests\test_notes_archive_stays_in_the_notes_folder.py",
        "tests\test_forgetting_is_applied_or_never_claimed_20261007.py",
        "tests\test_forget_subject_readers_r9.py",
        "tests\lifecycle\test_forget_revocation_law_20260929.py",
        "tests\test_email_store_recovery_surface.py",
        "tests\test_windows_default_local_model.py",
        "tests\test_windows_bundle_default_reaches_auto.py",
        "tests\test_windows_path_policy.py",
        "tests\test_windows_nonlocal_path_tool_calls.py",
        "tests\test_windows_nonlocal_path_doors.py",
        "tests\test_windows_nonlocal_path_routes.py",
        "tests\test_windows_nonlocal_path_commands.py",
        "tests\test_windows_nonlocal_path_message_lanes.py",
        "tests\test_windows_nonlocal_path_chat_page.py"
    )
    $previousUtf8 = $env:PYTHONUTF8
    $env:PYTHONUTF8 = "1"
    try {
        Invoke-Checked -Name "Windows runtime regression tests" -FilePath $python -Arguments (@("-m", "pytest") + $tests + @("-q", "--tb=short"))
    } finally {
        $env:PYTHONUTF8 = $previousUtf8
    }
}

function Invoke-ProviderProbe {
    $python = Resolve-Python
    Invoke-Checked -Name "Provider probe JSON" -FilePath $python -Arguments @("installer\provider_probe.py", "--json")

    if ($SkipBenchmark) {
        Add-Check -Name "Live local model benchmark" -Status "skipped" -ExitCode $null -DurationSeconds 0 -Detail "Skipped by flag."
    } else {
        Invoke-Checked -Name "Live local model benchmark" -FilePath $python -Arguments @("installer\provider_probe.py", "--benchmark", "--benchmark-timeout", [string]$BenchmarkTimeoutSeconds)
    }
}

function Invoke-PackageBuild {
    if ($SkipPackageBuild) {
        Add-Check -Name "Windows package build" -Status "skipped" -ExitCode $null -DurationSeconds 0 -Detail "Skipped by flag."
        return
    }

    $version = $PackageVersion
    if ([string]::IsNullOrWhiteSpace($version)) {
        $version = "gauntlet-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    }

    Invoke-Checked -Name "Windows package build" -FilePath "powershell" -Arguments @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "installer\build_windows_package.ps1",
        "-PackageVersion",
        $version
    )
}

function Invoke-OpenClawChecks {
    $openclaw = Resolve-Tool "openclaw"
    if ([string]::IsNullOrWhiteSpace($openclaw)) {
        if ($RequireOpenClaw) {
            throw "OpenClaw CLI was not found on PATH."
        }
        Add-Check -Name "OpenClaw CLI" -Status "skipped" -ExitCode $null -DurationSeconds 0 -Detail "OpenClaw CLI was not found on PATH."
        return
    }

    Invoke-Checked -Name "OpenClaw config validate" -FilePath $openclaw -Arguments @("config", "validate")
    Invoke-Checked -Name "OpenClaw doctor" -FilePath $openclaw -Arguments @("doctor", "--non-interactive", "--no-workspace-suggestions")
    Invoke-Checked -Name "OpenClaw gateway health" -FilePath $openclaw -Arguments @("gateway", "health")
    Invoke-Checked -Name "OpenClaw agents list" -FilePath $openclaw -Arguments @("agents", "list")
    Invoke-Checked -Name "OpenClaw memory status" -FilePath $openclaw -Arguments @("memory", "status", "--deep")
    Invoke-Checked -Name "OpenClaw VOOL exact response" -FilePath $openclaw -Arguments @("agent", "--agent", "vool", "--message", "Reply exactly OPENCLAW_VOOL_OK", "--json", "--timeout", "240")
}

function Complete-Run {
    param(
        [string]$Status,
        [string]$Message
    )

    $summary = [ordered]@{
        repository = "vool-local"
        schema = "vool.windows_fresh_host_gauntlet.v1"
        status = $Status
        message = $Message
        startedAt = $StartedAt.ToString("o")
        completedAt = (Get-Date).ToString("o")
        installProfile = $InstallProfile
        voolHome = $VoolHome
        checks = $Checks
    }
    $reportPath = Join-Path $ReportDir ("gauntlet-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8

    if ($Json) {
        $summary | ConvertTo-Json -Depth 8
    } else {
        Write-Host ""
        Write-Host "Windows fresh-host gauntlet $Status."
        Write-Host $Message
        Write-Host "Report: $reportPath"
    }
}

try {
    Invoke-Install
    Invoke-FocusedRegression
    Invoke-WindowsRuntimeRegression
    Invoke-ProviderProbe
    Invoke-PackageBuild
    Invoke-OpenClawChecks
    Complete-Run -Status "passed" -Message "All requested fresh-host checks passed."
    exit 0
} catch {
    Add-Check -Name "Failure" -Status "failed" -ExitCode 1 -DurationSeconds 0 -Detail $_.Exception.Message
    Complete-Run -Status "failed" -Message $_.Exception.Message
    exit 1
}
