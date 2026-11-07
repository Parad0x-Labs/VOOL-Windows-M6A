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
             the job launcher {app}\broker\confine matches its pin {app}\config\confine-pin.json,
             checked by the INSTALLED python and sandbox code (tools\confine_pin_check.py), with
             negative controls on a copy: planted DLL, changed byte, missing file, missing pin,
             swapped folder, and (Windows) a held file that can't be written or renamed
  launch     /healthz 200; the 11435 listener is VOOL's own python under the install dir, owned
             by the installing user; the window host is still alive after the settle period
  slices     tools/installed_slices.py against the installed API: health and version identity,
             each with a negative control (slices.json)
             then API recovery: the listener is killed and the supervisor must start a new one
  restart    stop leaves no owned process and a free port; relaunch is healthy again; the data
             directory persisted
  uninstall  a foreign listener survives uninstall; program files removed; no VOOL process left;
             user data preserved
Release use (release-windows.yml): -BuildOnlyTo <dir> builds the installer, checks its stamped
commit against -ExpectedCommit, copies VOOL-Setup.exe and provenance.json into <dir> and stops
before installing. A later run with -SetupPath <dir>\VOOL-Setup.exe skips the build and puts that
exact file through every other phase, checking the installed commit against -ExpectedCommit.
Limits, stated rather than implied: this runs as the runner account, not a separate standard
user (that machinery arrives with the broker work), and it proves process/HTTP lifecycle, not a
visible usable desktop or a local-model answer (those are the VM gate).
#>
[CmdletBinding()]
param(
    [string]$OllamaVersion = "v0.35.1",
    [string]$OllamaZipSha256 = "dc50b9ca7f9023c86525012632cd1615b093d0407987444a7f62ecab617e8e93",
    [int]$HealthTimeoutSeconds = 240,
    [int]$WindowSettleSeconds = 30,
    [string]$SetupPath = "",
    [string]$BuildOnlyTo = "",
    [string]$ExpectedCommit = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Convert-Path (Join-Path $PSScriptRoot "..")
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("vool-install-smoke-" + [guid]::NewGuid().ToString("N"))
$ReportDir = Join-Path $RepoRoot "dist\install-smoke"
New-Item -ItemType Directory -Force $Work, $ReportDir | Out-Null
# A slices.json left by an earlier run on this checkout must not read as this run's result.
Remove-Item -LiteralPath (Join-Path $ReportDir "slices.json") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $ReportDir "confine-pin.json") -Force -ErrorAction SilentlyContinue
$Checks = [System.Collections.Generic.List[object]]::new()
$InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\VOOL"
$DataRoot = Join-Path $env:LOCALAPPDATA "VOOL"
$ApiPort = 11435
$Foreign = $null
if ($SetupPath -and $BuildOnlyTo) { throw "-SetupPath and -BuildOnlyTo are separate runs" }
if ($ExpectedCommit -and $ExpectedCommit -notmatch '^[0-9a-f]{40}$') { throw "-ExpectedCommit must be a full 40-character commit" }

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
    # @(): a function's empty array returns as $null, and $null.Count throws under StrictMode.
    while ((Get-Date) -lt $deadline -and (@(Get-VoolProcesses).Count -gt 0 -or (Get-Listener $ApiPort))) { Start-Sleep -Seconds 2 }
}

function Start-Vool {
    # Exactly what the desktop shortcut runs.
    Start-Process -FilePath (Join-Path $env:WINDIR "System32\wscript.exe") -ArgumentList ('"' + (Join-Path $InstallRoot "vool.vbs") + '"') | Out-Null
}

$status = "passed"
$failure = ""
try {
    # --- build -----------------------------------------------------------------------------
    $stamped = $null
    $appVersion = ""
    if ($SetupPath) {
        $setup = Convert-Path -LiteralPath $SetupPath
        Require "build" "installer_given" (Test-Path -LiteralPath $setup -PathType Leaf) ("VOOL-Setup.exe sha256=" + (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash)
    } else {
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
        $stamped = Get-Content -Raw (Join-Path $stage "app\config\build-source.json") | ConvertFrom-Json
        if ($ExpectedCommit) {
            Require "build" "stamped_commit_is_expected" ([string]$stamped.commit_full -eq $ExpectedCommit) "stamped=$($stamped.commit_full) expected=$ExpectedCommit"
        }
    }

    if ($BuildOnlyTo) {
        # Release build: keep the exact installer and what it was built from, and install nothing.
        New-Item -ItemType Directory -Force $BuildOnlyTo | Out-Null
        $kept = Join-Path $BuildOnlyTo "VOOL-Setup.exe"
        Copy-Item -LiteralPath $setup -Destination $kept -Force
        $keptSha = (Get-FileHash -LiteralPath $kept -Algorithm SHA256).Hash.ToLowerInvariant()
        [ordered]@{
            schema = "vool.windows_release_provenance.v1"
            commit = [string]$stamped.commit_full
            dirty_state = [bool]$stamped.dirty_state
            version = $appVersion
            installer = "VOOL-Setup.exe"
            installer_sha256 = $keptSha
            installer_bytes = (Get-Item -LiteralPath $kept).Length
            ollama = $OllamaVersion
            ollama_zip_sha256 = $OllamaZipSha256
            authenticode = "unsigned"
        } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $BuildOnlyTo "provenance.json")
        Add-Check "build" "installer_kept" $true "$kept sha256=$keptSha"
    } else {
        # --- install with a foreign listener on the API port ------------------------------------
        $Foreign = Start-ForeignListener
        $installLog = Join-Path $ReportDir "install.log"
        $installStart = Get-Date
        $p = Start-Process -FilePath $setup -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$installLog`"") -PassThru -Wait
        Require "install" "setup_exit_code" ($p.ExitCode -eq 0) "exit=$($p.ExitCode)"
        $stillThere = Get-Process -Id $Foreign.Id -ErrorAction SilentlyContinue
        $owner = Get-Listener $ApiPort
        Require "install" "foreign_listener_survived" ([bool]$stillThere -and $owner -and $owner.ProcessId -eq $Foreign.Id) "pid=$($Foreign.Id)"
        Require "install" "per_user_install_dir" (Test-Path (Join-Path $InstallRoot "vool.vbs")) $InstallRoot
        if ($ExpectedCommit) {
            $installed = Get-Content -Raw (Join-Path $InstallRoot "app\config\build-source.json") | ConvertFrom-Json
            Require "install" "installed_commit_is_expected" ([string]$installed.commit_full -eq $ExpectedCommit) "installed=$($installed.commit_full) expected=$ExpectedCommit"
        }
        $logText = Get-Content -Raw -LiteralPath $installLog
        Require "install" "not_elevated_by_setup" ($logText -match "(?im)Administrative install mode:\s*No") "Inno log: non-administrative install mode"
        # The job launcher ships in its own pinned folder. The installed python runs the installed pin
        # check, so this proves what the installed app would accept; the controls run on a copy.
        $installedPython = Join-Path $InstallRoot "python\python.exe"
        $pinWork = Join-Path $Work "confine-pin"
        New-Item -ItemType Directory -Force $pinWork | Out-Null
        $ErrorActionPreference = "Continue"
        & $installedPython -I (Join-Path $RepoRoot "tools\confine_pin_check.py") --app-root (Join-Path $InstallRoot "app") --work $pinWork --out (Join-Path $ReportDir "confine-pin.json") 2>&1 |
            Out-File (Join-Path $ReportDir "confine-pin.log")
        $pinExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        Require "install" "confine_launcher_pinned" ($pinExit -eq 0) "exit=$pinExit; see confine-pin.json"
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
        # Installed slices (tools/installed_slices.py): each drives the installed API over loopback with
        # a negative control, into slices.json. Without -ExpectedCommit the checkout's own commit is
        # expected, which is what a build-and-install run built.
        $sliceCommit = $(if ($ExpectedCommit) { $ExpectedCommit } else { (& git -C $RepoRoot rev-parse HEAD).Trim() })
        # Windows PowerShell 5.1 turns a native command's stderr into a terminating error under "Stop";
        # the exit code is the verdict here, so stderr only goes to the log.
        $ErrorActionPreference = "Continue"
        & (Get-Command python).Source (Join-Path $RepoRoot "tools\installed_slices.py") --base-url "http://127.0.0.1:$ApiPort" --expected-commit $sliceCommit --out (Join-Path $ReportDir "slices.json") 2>&1 |
            Out-File (Join-Path $ReportDir "slices.log")
        $slicesExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        Require "slices" "installed_slices_pass" ($slicesExit -eq 0) "expected commit $sliceCommit; see slices.json"
        Start-Sleep -Seconds $WindowSettleSeconds
        $window = @(Get-VoolProcesses | Where-Object { $_.CommandLine -like "*vool_window.py*" })
        Require "launch" "window_host_alive_after_settle" ($window.Count -ge 1) "window processes=$($window.Count) after ${WindowSettleSeconds}s"

        # --- installed slice: API recovery (Step 4.2) -------------------------------------------
        # Kill the API listener proved above to be VOOL's own python; the bundle supervisor must start a
        # new one. Negative control: the killed pid is gone, so a healthy answer afterwards cannot be it.
        $killedApi = $listener.ProcessId
        Stop-Process -Id $killedApi -Force
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and (Get-Process -Id $killedApi -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 500 }
        Require "slices" "api_recovery_control_killed_pid_gone" (-not (Get-Process -Id $killedApi -ErrorAction SilentlyContinue)) "pid=$killedApi"
        Require "slices" "api_recovered_healthz_200" (Wait-Health $HealthTimeoutSeconds) "after killing pid $killedApi"
        $recovered = Get-Listener $ApiPort
        Require "slices" "api_recovered_as_new_vools_python" ($recovered -and $recovered.ProcessId -ne $killedApi -and $recovered.ExecutablePath -and $recovered.ExecutablePath.StartsWith($InstallRoot + "\", [StringComparison]::OrdinalIgnoreCase) -and $recovered.CommandLine -like "*vool_api_server.py*") $(if ($recovered) { "pid=$($recovered.ProcessId) $($recovered.ExecutablePath)" } else { "no listener" })

        # --- restart -------------------------------------------------------------------------
        Stop-Vool
        $left = @(Get-VoolProcesses)
        Require "restart" "no_owned_process_after_stop" ($left.Count -eq 0) (($left | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ",")
        Require "restart" "api_port_free_after_stop" (-not (Get-Listener $ApiPort)) "$ApiPort"
        Start-Vool
        Require "restart" "healthz_200_after_relaunch" (Wait-Health $HealthTimeoutSeconds) "relaunch"
        Require "restart" "data_dir_persisted" (Test-Path $DataRoot) $DataRoot
        Stop-Vool

        # --- uninstall with a foreign listener ----------------------------------------------------
        # The uninstaller deletes {app}\app outright (vool.iss [UninstallDelete]) to take the runtime's
        # bytecode with it, so the runtime must keep its state out of the program folder: anything it
        # wrote there besides .pyc would be destroyed. Installed files keep their source timestamps.
        $runtimeWritten = @(Get-ChildItem -LiteralPath (Join-Path $InstallRoot "app") -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ne ".pyc" -and $_.LastWriteTime -gt $installStart })
        Require "uninstall" "program_dir_holds_no_runtime_state" ($runtimeWritten.Count -eq 0) (($runtimeWritten | Select-Object -First 10 | ForEach-Object { $_.FullName.Substring($InstallRoot.Length) }) -join ",")
        $Foreign = Start-ForeignListener
        $uninstaller = Join-Path $InstallRoot "unins000.exe"
        $u = Start-Process -FilePath $uninstaller -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$(Join-Path $ReportDir 'uninstall.log')`"") -PassThru -Wait
        Require "uninstall" "uninstaller_exit_code" ($u.ExitCode -eq 0) "exit=$($u.ExitCode)"
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline -and (Test-Path (Join-Path $InstallRoot "app"))) { Start-Sleep -Seconds 2 }
        Require "uninstall" "foreign_listener_survived" ([bool](Get-Process -Id $Foreign.Id -ErrorAction SilentlyContinue)) "pid=$($Foreign.Id)"
        Require "uninstall" "program_files_removed" (-not (Test-Path (Join-Path $InstallRoot "app"))) $InstallRoot
        Require "uninstall" "confine_launcher_removed" (-not (Test-Path (Join-Path $InstallRoot "broker\confine")) -and -not (Test-Path (Join-Path $InstallRoot "config\confine-pin.json"))) "$InstallRoot\broker\confine"
        $left = @(Get-VoolProcesses)
        Require "uninstall" "no_vool_process_left" ($left.Count -eq 0) (($left | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ",")
        Add-Check "uninstall" "user_data_preserved" (Test-Path $DataRoot) $DataRoot
    }
} catch {
    $status = "failed"
    $failure = $_.Exception.Message
    Write-Host "SMOKE FAILED: $failure"
} finally {
    if ($Foreign) { Stop-Process -Id $Foreign.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$DataRoot\logs") { Copy-Item -Recurse -Force "$DataRoot\logs" (Join-Path $ReportDir "vool-data-logs") -ErrorAction SilentlyContinue }
    # The window host logs to its native state dir (core.runtime_paths.native_state_dir), and the
    # API routes its runtime log to data\logs\vool_api.log: neither lives in logs\.
    if (Test-Path "$DataRoot\open.log") { Copy-Item -Force "$DataRoot\open.log" (Join-Path $ReportDir "window-open.log") -ErrorAction SilentlyContinue }
    if (Test-Path "$DataRoot\data\logs") { Copy-Item -Recurse -Force "$DataRoot\data\logs" (Join-Path $ReportDir "vool-runtime-logs") -ErrorAction SilentlyContinue }
    # A slices.json that does not parse must not cost the report itself: it is kept as its own file.
    $slicesReport = $null
    $confinePinReport = $null
    try {
        if (Test-Path (Join-Path $ReportDir "confine-pin.json")) { $confinePinReport = Get-Content -Raw (Join-Path $ReportDir "confine-pin.json") | ConvertFrom-Json }
    } catch {
        $confinePinReport = "unreadable: $($_.Exception.Message)"
    }
    try {
        if (Test-Path (Join-Path $ReportDir "slices.json")) { $slicesReport = Get-Content -Raw (Join-Path $ReportDir "slices.json") | ConvertFrom-Json }
    } catch {
        $slicesReport = "unreadable: $($_.Exception.Message)"
    }
    $report = [ordered]@{
        schema = "vool.windows_install_smoke.v1"
        status = $status
        failure = $failure
        ollama = $OllamaVersion
        mode = $(if ($BuildOnlyTo) { "build_only" } elseif ($SetupPath) { "given_installer" } else { "build_and_install" })
        expected_commit = $ExpectedCommit
        runner_account = $env:USERNAME
        limits = @("runner account, not a separate standard user", "process/HTTP lifecycle only; no visible-desktop or local-model proof")
        checks = $Checks
        slices = $slicesReport
        confine_pin = $confinePinReport
    }
    $report | ConvertTo-Json -Depth 10 | Set-Content -Encoding utf8 (Join-Path $ReportDir "report.json")
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
if ($status -ne "passed") { exit 1 }
exit 0
