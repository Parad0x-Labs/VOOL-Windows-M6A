<#
Installed-app smoke for the Windows bundle: build the real Inno Setup installer, install it,
launch it the way the desktop shortcut does, and prove the lifecycle against measurable checks.

Run on a disposable Windows host (the windows-install-smoke workflow on windows-latest). It
installs VOOL for the current user, starts and stops processes, and binds 127.0.0.1:11435, so do
not run it on a machine whose VOOL install or port you care about.

Checks (each recorded PASS/FAIL in dist\install-smoke\report.json; the first hard failure stops
the run, and logs are always kept):
  build      Ollama release hash-verified; bundle staged; installer compiled
  install    a FOREIGN listener on 11435 survives install; non-elevated per-user install dir
  launch     /healthz 200; the 11435 listener is VOOL's own python under the install dir, owned
             by the installing user; the window host is still alive after the settle period
  restart    stop leaves no owned process and a free port; relaunch is healthy again; the data
             directory persisted
  uninstall  a foreign listener survives uninstall; program files removed; no VOOL process left;
             user data preserved
Limits, stated rather than implied: this runs as the runner account, not a separate standard
user (that machinery arrives with the broker work), and it proves process/HTTP lifecycle, not a
visible usable desktop or a local-model answer (those are the VM gate).
#>
[CmdletBinding()]
param(
    [string]$OllamaVersion = "v0.35.1",
    [string]$OllamaZipSha256 = "dc50b9ca7f9023c86525012632cd1615b093d0407987444a7f62ecab617e8e93",
    [int]$HealthTimeoutSeconds = 240,
    [int]$WindowSettleSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Convert-Path (Join-Path $PSScriptRoot "..")
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("vool-install-smoke-" + [guid]::NewGuid().ToString("N"))
$ReportDir = Join-Path $RepoRoot "dist\install-smoke"
New-Item -ItemType Directory -Force $Work, $ReportDir | Out-Null
$Checks = [System.Collections.Generic.List[object]]::new()
$InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\VOOL"
$DataRoot = Join-Path $env:LOCALAPPDATA "VOOL"
$ApiPort = 11435
$Foreign = $null

function Add-Check([string]$Phase, [string]$Name, [bool]$Passed, [string]$Detail = "") {
    $Checks.Add([pscustomobject]@{ phase = $Phase; name = $Name; status = $(if ($Passed) { "PASS" } else { "FAIL" }); detail = $Detail })
    Write-Host ("[{0}] {1}/{2} {3}" -f $(if ($Passed) { "PASS" } else { "FAIL" }), $Phase, $Name, $Detail)
}

function Require([string]$Phase, [string]$Name, [bool]$Passed, [string]$Detail = "") {
    Add-Check $Phase $Name $Passed $Detail
    if (-not $Passed) { throw "$Phase/$Name failed: $Detail" }
}

function Get-Listener([int]$Port) {
    $conn = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
}

function Get-VoolProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot + "\", [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and ($_.CommandLine -like "*vool_api_server.py*" -or $_.CommandLine -like "*vool_window.py*" -or $_.CommandLine -like "*bundle_supervisor.py*"))
    })
}

function Wait-Health([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$ApiPort/healthz"
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 3
    }
    return $false
}

function Start-ForeignListener {
    # A listener that is NOT VOOL: system Python's http.server outside the install dir.
    $p = Start-Process -FilePath (Get-Command python).Source -ArgumentList @("-m", "http.server", "$ApiPort", "--bind", "127.0.0.1") `
        -WorkingDirectory $Work -PassThru -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $l = Get-Listener $ApiPort
        if ($l -and $l.ProcessId -eq $p.Id) { return $p }
        Start-Sleep -Milliseconds 500
    }
    throw "foreign listener did not bind $ApiPort"
}

function Stop-Vool {
    $supervisor = Join-Path $InstallRoot "bundle_supervisor.py"
    $python = Join-Path $InstallRoot "python\python.exe"
    if ((Test-Path $supervisor) -and (Test-Path $python)) {
        & $python $supervisor --stop --root $InstallRoot 2>&1 | Out-File -Append (Join-Path $ReportDir "stop.log")
    }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and ((Get-VoolProcesses).Count -gt 0 -or (Get-Listener $ApiPort))) { Start-Sleep -Seconds 2 }
}

function Start-Vool {
    # Exactly what the desktop shortcut runs.
    Start-Process -FilePath (Join-Path $env:WINDIR "System32\wscript.exe") -ArgumentList ('"' + (Join-Path $InstallRoot "vool.vbs") + '"') | Out-Null
}

$status = "passed"
$failure = ""
try {
    # --- build -----------------------------------------------------------------------------
    $zip = Join-Path $Work "ollama-windows-amd64.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/ollama/ollama/releases/download/$OllamaVersion/ollama-windows-amd64.zip" -OutFile $zip
    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    Require "build" "ollama_release_hash" ($actual -eq $OllamaZipSha256) "$OllamaVersion sha256=$actual"
    $ollamaDir = Join-Path $Work "ollama"
    Expand-Archive -LiteralPath $zip -DestinationPath $ollamaDir
    Remove-Item -LiteralPath $zip
    $stage = Join-Path $Work "stage"
    & (Join-Path $RepoRoot "installer\bundle\build_bundle.ps1") -Stage $stage -OllamaExe (Join-Path $ollamaDir "ollama.exe") *>&1 |
        Out-File (Join-Path $ReportDir "build_bundle.log")
    Require "build" "bundle_staged" (Test-Path (Join-Path $stage "bundle_manifest.json")) $stage
    $appVersion = (& (Join-Path $stage "python\python.exe") -c "import sys; sys.path.insert(0, r'$stage\app'); from core.app_version import VOOL_VERSION; print(VOOL_VERSION)").Trim()
    $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    Require "build" "inno_setup_present" (Test-Path $iscc) $iscc
    $outDir = Join-Path $Work "dist"
    & $iscc "/DStage=$stage" "/DOutDir=$outDir" "/DAppVersion=$appVersion" (Join-Path $RepoRoot "installer\bundle\vool.iss") *>&1 |
        Out-File (Join-Path $ReportDir "iscc.log")
    $setup = Join-Path $outDir "VOOL-Setup.exe"
    Require "build" "installer_compiled" (Test-Path $setup) ("VOOL-Setup.exe sha256=" + (Get-FileHash $setup -Algorithm SHA256).Hash + " version=$appVersion")

    # --- install with a foreign listener on the API port ------------------------------------
    $Foreign = Start-ForeignListener
    $installLog = Join-Path $ReportDir "install.log"
    $p = Start-Process -FilePath $setup -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$installLog`"") -PassThru -Wait
    Require "install" "setup_exit_code" ($p.ExitCode -eq 0) "exit=$($p.ExitCode)"
    $stillThere = Get-Process -Id $Foreign.Id -ErrorAction SilentlyContinue
    $owner = Get-Listener $ApiPort
    Require "install" "foreign_listener_survived" ([bool]$stillThere -and $owner -and $owner.ProcessId -eq $Foreign.Id) "pid=$($Foreign.Id)"
    Require "install" "per_user_install_dir" (Test-Path (Join-Path $InstallRoot "vool.vbs")) $InstallRoot
    $logText = Get-Content -Raw -LiteralPath $installLog
    Require "install" "not_elevated_by_setup" ($logText -match "(?im)Administrative install mode:\s*No") "Inno log: non-administrative install mode"
    Stop-Process -Id $Foreign.Id -Force
    Start-Sleep -Seconds 2
    $Foreign = $null

    # --- launch --------------------------------------------------------------------------
    Start-Vool
    Require "launch" "healthz_200" (Wait-Health $HealthTimeoutSeconds) "http://127.0.0.1:$ApiPort/healthz"
    $listener = Get-Listener $ApiPort
    $ownerUser = (Invoke-CimMethod -InputObject $listener -MethodName GetOwner).User
    Require "launch" "listener_is_vools_python" ($listener.ExecutablePath.StartsWith($InstallRoot + "\", [StringComparison]::OrdinalIgnoreCase) -and $listener.CommandLine -like "*vool_api_server.py*") $listener.ExecutablePath
    Require "launch" "listener_owned_by_installing_user" ($ownerUser -eq $env:USERNAME) "owner=$ownerUser"
    Start-Sleep -Seconds $WindowSettleSeconds
    $window = @(Get-VoolProcesses | Where-Object { $_.CommandLine -like "*vool_window.py*" })
    Require "launch" "window_host_alive_after_settle" ($window.Count -ge 1) "window processes=$($window.Count) after ${WindowSettleSeconds}s"

    # --- restart -------------------------------------------------------------------------
    Stop-Vool
    $left = Get-VoolProcesses
    Require "restart" "no_owned_process_after_stop" ($left.Count -eq 0) (($left | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ",")
    Require "restart" "api_port_free_after_stop" (-not (Get-Listener $ApiPort)) "$ApiPort"
    Start-Vool
    Require "restart" "healthz_200_after_relaunch" (Wait-Health $HealthTimeoutSeconds) "relaunch"
    Require "restart" "data_dir_persisted" (Test-Path $DataRoot) $DataRoot
    Stop-Vool

    # --- uninstall with a foreign listener ----------------------------------------------------
    $Foreign = Start-ForeignListener
    $uninstaller = Join-Path $InstallRoot "unins000.exe"
    $u = Start-Process -FilePath $uninstaller -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$(Join-Path $ReportDir 'uninstall.log')`"") -PassThru -Wait
    Require "uninstall" "uninstaller_exit_code" ($u.ExitCode -eq 0) "exit=$($u.ExitCode)"
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and (Test-Path (Join-Path $InstallRoot "app"))) { Start-Sleep -Seconds 2 }
    Require "uninstall" "foreign_listener_survived" ([bool](Get-Process -Id $Foreign.Id -ErrorAction SilentlyContinue)) "pid=$($Foreign.Id)"
    Require "uninstall" "program_files_removed" (-not (Test-Path (Join-Path $InstallRoot "app"))) $InstallRoot
    $left = Get-VoolProcesses
    Require "uninstall" "no_vool_process_left" ($left.Count -eq 0) (($left | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ",")
    Add-Check "uninstall" "user_data_preserved" (Test-Path $DataRoot) $DataRoot
} catch {
    $status = "failed"
    $failure = $_.Exception.Message
    Write-Host "SMOKE FAILED: $failure"
} finally {
    if ($Foreign) { Stop-Process -Id $Foreign.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$DataRoot\logs") { Copy-Item -Recurse -Force "$DataRoot\logs" (Join-Path $ReportDir "vool-data-logs") -ErrorAction SilentlyContinue }
    $report = [ordered]@{
        schema = "vool.windows_install_smoke.v1"
        status = $status
        failure = $failure
        ollama = $OllamaVersion
        runner_account = $env:USERNAME
        limits = @("runner account, not a separate standard user", "process/HTTP lifecycle only; no visible-desktop or local-model proof")
        checks = $Checks
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $ReportDir "report.json")
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
if ($status -ne "passed") { exit 1 }
exit 0
