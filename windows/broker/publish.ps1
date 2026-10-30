<#
Publishes the broker self-contained for win-x64, so it runs with no .NET installed (about 75 MB).
The output is what installer\bundle\build_bundle.ps1 ships to {app}\broker, and what a source
checkout's API starts from windows\broker\publish.

Usage:
  powershell -File windows\broker\publish.ps1
  powershell -File windows\broker\publish.ps1 -Dotnet C:\path\to\dotnet.exe -Source C:\path\to\packages
#>
param(
  [string]$Output = (Join-Path $PSScriptRoot "publish"),
  [string]$Dotnet = "dotnet",
  # A local package folder for an offline restore. Empty means the configured NuGet sources.
  [string]$Source = ""
)
$ErrorActionPreference = "Stop"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$project = Join-Path $PSScriptRoot "src\VoolBroker\VoolBroker.csproj"
# The runtime is pinned to the one SDK 10.0.402 (global.json) bundles. The broker references
# neither ASP.NET Core nor Windows Desktop, so their runtime packs are not downloaded.
$publishArgs = @(
  "publish", $project,
  "-c", "Release",
  "-r", "win-x64",
  "--self-contained",
  "-p:RuntimeFrameworkVersion=10.0.13",
  "-p:DisableTransitiveFrameworkReferenceDownloads=true",
  "-o", $Output
)
if ($Source) { $publishArgs += @("--source", $Source) }
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
