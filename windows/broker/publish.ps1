<#
Publishes the broker self-contained for win-x64, so it runs with no .NET installed (about 75 MB).
The output is what installer\bundle\build_bundle.ps1 ships to {app}\broker, and what a source
checkout's API starts from windows\broker\publish.

It also publishes the job launcher VoolConfine, self-contained, into its own folder <Output>\confine,
and writes the pin manifest for that folder (docs\WINDOWS_CONFINEMENT.md section 2): every file in
<Output>\confine with its SHA-256. VOOL runs the launcher only when the folder holds exactly those
files (sandbox\windows_confinement_pin.py). The pin goes to <Output>\confine-pin.json, or to -PinPath
(the bundle puts it in <Stage>\config, installed as {app}\config\confine-pin.json).

Usage:
  powershell -File windows\broker\publish.ps1
  powershell -File windows\broker\publish.ps1 -Dotnet C:\path\to\dotnet.exe -Source C:\path\to\packages
#>
param(
  [string]$Output = (Join-Path $PSScriptRoot "publish"),
  [string]$Dotnet = "dotnet",
  # A local package folder for an offline restore. Empty means the configured NuGet sources.
  [string]$Source = "",
  # Where the VoolConfine pin manifest goes. Empty means <Output>\confine-pin.json.
  [string]$PinPath = ""
)
$ErrorActionPreference = "Stop"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$project = Join-Path $PSScriptRoot "src\VoolBroker\VoolBroker.csproj"
# The runtime is pinned to the one SDK 10.0.402 (global.json) bundles. The broker references
# neither ASP.NET Core nor Windows Desktop, so their runtime packs are not downloaded.
$runtimeArgs = @(
  "-c", "Release",
  "-r", "win-x64",
  "--self-contained",
  "-p:RuntimeFrameworkVersion=10.0.13",
  "-p:DisableTransitiveFrameworkReferenceDownloads=true"
)
if ($Source) { $runtimeArgs += @("--source", $Source) }
$publishArgs = @("publish", $project) + $runtimeArgs + @("-o", $Output)
# Only the default output folder (git-ignored, beside this script) is cleared before a publish.
# Any other folder must be empty, so a mistyped -Output never deletes anything.
$defaultOutput = Join-Path $PSScriptRoot "publish"
if (Test-Path -LiteralPath $Output) {
  if ($Output -eq $defaultOutput) {
    Remove-Item -LiteralPath $Output -Recurse -Force
  } elseif (Get-ChildItem -LiteralPath $Output -Force | Select-Object -First 1) {
    throw "-Output $Output is not empty"
  }
}
& $Dotnet @publishArgs
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed with exit code $LASTEXITCODE" }
$exe = Join-Path $Output "VoolBroker.exe"
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "publish produced no VoolBroker.exe in $Output" }
if (-not (Test-Path -LiteralPath (Join-Path $Output "coreclr.dll") -PathType Leaf)) {
  throw "publish in $Output is not self-contained (no coreclr.dll)"
}
Write-Host "Broker published: $exe"

# --- VoolConfine, the job launcher, in its own pinned folder ---------------------------------
# Its own folder, so the pin covers exactly the files it loads (its DLLs and its runtime) and an
# unlisted file there, such as a planted DLL, is refused. StartupHookSupport=false: the runtime
# then ignores DOTNET_STARTUP_HOOKS, so no environment variable can load code into the launcher.
$confineProject = Join-Path $PSScriptRoot "src\VoolConfine\VoolConfine.csproj"
$confineDir = Join-Path $Output "confine"
if (-not $PinPath) { $PinPath = Join-Path $Output "confine-pin.json" }
# A pin left by an earlier publish must never describe this one's files, even if this one fails.
if (Test-Path -LiteralPath $PinPath) { Remove-Item -LiteralPath $PinPath -Force }
$confineArgs = @("publish", $confineProject) + $runtimeArgs + @("-p:StartupHookSupport=false", "-o", $confineDir)
& $Dotnet @confineArgs
if ($LASTEXITCODE -ne 0) { throw "dotnet publish of VoolConfine failed with exit code $LASTEXITCODE" }
$confineExe = Join-Path $confineDir "VoolConfine.exe"
if (-not (Test-Path -LiteralPath $confineExe -PathType Leaf)) { throw "publish produced no VoolConfine.exe in $confineDir" }
if (-not (Test-Path -LiteralPath (Join-Path $confineDir "coreclr.dll") -PathType Leaf)) {
  throw "VoolConfine publish in $confineDir is not self-contained (no coreclr.dll)"
}
$confineRoot = (Get-Item -LiteralPath $confineDir).FullName.TrimEnd("\")
$pinFull = [System.IO.Path]::GetFullPath($PinPath)
if ($pinFull.StartsWith($confineRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "-PinPath $PinPath is inside $confineDir, where it would be an unlisted file"
}
$pinned = [ordered]@{}
$items = @(Get-ChildItem -LiteralPath $confineRoot -Recurse -Force)
foreach ($item in $items) {
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw "VoolConfine publish holds a link: $($item.FullName)" }
}
$files = @($items | Where-Object { -not $_.PSIsContainer } | Sort-Object { $_.FullName.Substring($confineRoot.Length + 1).Replace("\", "/") } -CaseSensitive)
foreach ($file in $files) {
  $relative = $file.FullName.Substring($confineRoot.Length + 1).Replace("\", "/")
  $pinned[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
if (-not $pinned.Contains("VoolConfine.exe")) { throw "the VoolConfine pin does not list VoolConfine.exe" }
$pin = [ordered]@{ schema = "vool.windows_confinement.pin.v1"; files = $pinned }
$pinParent = Split-Path -Parent $pinFull
if (-not (Test-Path -LiteralPath $pinParent)) { New-Item -ItemType Directory -Force $pinParent | Out-Null }
# UTF-8 without a byte-order mark (Windows PowerShell 5.1's Set-Content utf8 would add one).
[System.IO.File]::WriteAllText($pinFull, ($pin | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
Write-Host "VoolConfine published: $confineExe ($($pinned.Count) files pinned in $pinFull)"
