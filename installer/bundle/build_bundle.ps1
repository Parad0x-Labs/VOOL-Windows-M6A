<#
Assembles a self-contained VOOL bundle staging directory: a relocatable embedded Python with
the lean runtime deps (no heavy ML training stack -- that is training-only), the VOOL source,
the bundled Ollama binary, the launcher, and optionally the local model. Feed the resulting
$Stage directory to Inno Setup (vool.iss) to produce VOOL-Setup.exe.

Layout produced:
  <Stage>\python\        embedded Python + Lib\site-packages (lean deps)
  <Stage>\app\           VOOL source (apps, core, adapters, config)
  <Stage>\ollama\        ollama.exe + lib\ollama\ runner support files
  <Stage>\models\        Ollama model store (populated only with -IncludeModel)
  <Stage>\VOOL.cmd      launcher

Usage:
  pwsh installer\bundle\build_bundle.ps1 -Stage G:\vool-build\bundle
  pwsh installer\bundle\build_bundle.ps1 -Stage G:\vool-build\bundle -IncludeModel   # multi-GB offline bundle
#>
param(
  [string]$Stage = "G:\vool-build\bundle",
  [string]$PythonVersion = "3.12.7",
  [string]$OllamaExe = "G:\Ollama\ollama.exe",
  [string]$WebView2Installer = "",
  [string]$WebView2Sha256 = "",
  [string]$Model = "qwen2.5:7b",
  [ValidateSet("Internal", "Release")]
  [string]$BuildMode = "Internal",
  [string]$DotNetExe = "dotnet",
  [switch]$IncludeModel
)
$ErrorActionPreference = "Stop"

# Internal builds include approved edits to tracked files, never ambient checkout residue.
# Keep these helpers free of build/runtime side effects so staging can be tested separately.
function Copy-InternalBundleSource {
  param([string]$RepoRoot, [string]$AppDir, [string[]]$Paths)
  if (Test-Path -LiteralPath $AppDir) {
    if (Get-ChildItem -LiteralPath $AppDir -Force | Select-Object -First 1) {
      throw "Internal app stage must be empty; existing files will not be overwritten."
    }
  }
  $files = @(& git -C $RepoRoot -c core.quotePath=false ls-files -- $Paths)
  if ($LASTEXITCODE -ne 0 -or -not $files.Count) { throw "Tracked bundle source inventory failed." }
  foreach ($relativePath in $files) {
    $src = Join-Path $RepoRoot $relativePath
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
      throw "Tracked bundle source missing: $relativePath"
    }
    # Do not follow tracked symlinks or directory junctions outside the source inventory.
    $item = Get-Item -LiteralPath $src -Force
    while ($item.FullName -ne $RepoRoot) {
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse point in tracked bundle source: $relativePath"
      }
      $item = Get-Item -LiteralPath (Split-Path -Parent $item.FullName) -Force
    }
    $dest = Join-Path $AppDir $relativePath
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dest
  }
  return $files
}

function Assert-InternalBundlePayload {
  param([string]$RepoRoot, [string]$AppDir, [string[]]$SourceFiles)
  $allowed = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($relativePath in $SourceFiles) { [void]$allowed.Add($relativePath.Replace('\', '/')) }
  foreach ($item in Get-ChildItem -LiteralPath $AppDir -Recurse -Force) {
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      throw "Reparse point in app payload: $($item.FullName)"
    }
    if ($item.PSIsContainer) { continue }
    $relativePath = [System.IO.Path]::GetRelativePath($AppDir, $item.FullName).Replace('\', '/')
    if ($relativePath -eq 'config/build-source.json') { continue } # explicitly generated below
    if (-not $allowed.Contains($relativePath)) { throw "Unexpected app payload file: $relativePath" }
    if ((Get-FileHash -LiteralPath $item.FullName).Hash -ne
        (Get-FileHash -LiteralPath (Join-Path $RepoRoot $relativePath)).Hash) {
      throw "App payload differs from source: $relativePath"
    }
  }
  foreach ($relativePath in $allowed) {
    if (-not (Test-Path -LiteralPath (Join-Path $AppDir $relativePath) -PathType Leaf)) {
      throw "Missing app payload file: $relativePath"
    }
  }
}

function Write-PythonUpgradeInputs {
  param([string]$Stage, [string]$PythonDir, [string]$PythonExe, [string]$Helper)
  # Create the stdlib-only verifier BEFORE inventory: an embedded _pth import-site
  # directive can override -S. Never run the verifier in candidate site-packages.
  $bootstrap = Join-Path $Stage "python-upgrade-bootstrap"
  New-Item -ItemType Directory $bootstrap | Out-Null
  Get-ChildItem -LiteralPath $PythonDir -File | Where-Object { $_.Extension -ne "._pth" } |
    Copy-Item -Destination $bootstrap
  $stdlibZip = @(Get-ChildItem -LiteralPath $PythonDir -File -Filter "python*.zip")
  $pathFile = @(Get-ChildItem -LiteralPath $PythonDir -File -Filter "python*._pth")
  if ($stdlibZip.Count -ne 1 -or $pathFile.Count -ne 1) { throw "Ambiguous Python upgrade bootstrap layout." }
  # DLL-named _pth takes precedence over the executable's; explicitly omit import site.
  @($stdlibZip[0].Name, ".") | Set-Content -LiteralPath (Join-Path $bootstrap $pathFile[0].Name) -Encoding ascii
  Copy-Item -LiteralPath $Helper -Destination $bootstrap
  $verifier = Join-Path $bootstrap ([System.IO.Path]::GetFileName($PythonExe))
  & $verifier -I -S -B (Join-Path $bootstrap "upgrade_python_payload.py") inventory `
    --root $Stage --manifest (Join-Path $Stage "python_payload.json")
  if ($LASTEXITCODE -ne 0) { throw "Python payload ownership inventory failed." }
}

# `Resolve-Path`.Path can be provider-qualified when the checkout is reached through a WSL UNC
# path (for example, a PowerShell provider prefix before a UNC path). Embedded Python and the
# Windows file APIs need the provider path itself, so normalize through Convert-Path.
$RepoRoot = Convert-Path (Join-Path $PSScriptRoot "..\..")
$Stage = [System.IO.Path]::GetFullPath($Stage)
if (Test-Path -LiteralPath $Stage) {
  if (Get-ChildItem -LiteralPath $Stage -Force | Select-Object -First 1) {
    throw "Bundle stage must be empty. Choose a fresh staging directory; no existing files will be deleted."
  }
} else {
  New-Item -ItemType Directory -Force $Stage | Out-Null
}
Write-Host "Repo:  $RepoRoot"
Write-Host "Stage: $Stage"

$commitFull = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $commitFull -notmatch '^[0-9a-f]{40}$') {
  throw "Bundle source provenance is not an exact Git commit."
}
$dirty = -not [string]::IsNullOrWhiteSpace((@(& git -C $RepoRoot status --porcelain) -join "`n").Trim())
if ($BuildMode -eq "Release") {
  if ($dirty) { throw "Release bundle source checkout is dirty." }
  $auditPython = Join-Path $RepoRoot "..\toolchain\python-tests\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $auditPython)) { $auditPython = "python" }
  & $auditPython -m tools.windows_authority_audit --release-check
  if ($LASTEXITCODE -ne 0) { throw "Windows authority release gate failed." }
}

# Release payloads are assembled from the exact committed tree, never from the checkout.
# Internal builds intentionally use the working tree and are stamped non-release below.
$SourceRoot = $RepoRoot
$releaseArchiveRoot = $null
if ($BuildMode -eq "Release") {
  $releaseArchive = Join-Path $Stage ".release-source.zip"
  $releaseArchiveRoot = Join-Path $Stage ".release-source"
  & git -C $RepoRoot archive --format=zip --output=$releaseArchive $commitFull
  if ($LASTEXITCODE -ne 0) { throw "Could not create the exact release source archive." }
  New-Item -ItemType Directory -Force $releaseArchiveRoot | Out-Null
  Expand-Archive -LiteralPath $releaseArchive -DestinationPath $releaseArchiveRoot
  Remove-Item -LiteralPath $releaseArchive -Force
  $SourceRoot = $releaseArchiveRoot
}

# --- 1. embedded Python -------------------------------------------------------------------
$pyDir = Join-Path $Stage "python"
New-Item -ItemType Directory -Force $pyDir | Out-Null
$embedZip = Join-Path $Stage "python-embed.zip"
$embedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
Write-Host "Downloading embeddable Python $PythonVersion ..."
Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
Expand-Archive -Path $embedZip -DestinationPath $pyDir -Force
Remove-Item $embedZip -Force
# enable site-packages so pip-installed deps resolve
$pth = (Get-ChildItem "$pyDir\python*._pth" | Select-Object -First 1).FullName
$lines = Get-Content $pth | ForEach-Object { if ($_ -match '^\s*#\s*import site\s*$') { 'import site' } else { $_ } }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
Set-Content -Path $pth -Value $lines -Encoding ascii

# --- 2. lean runtime deps (no heavy ML training stack) ------------------------------------
$env:PIP_CACHE_DIR = Join-Path $Stage "..\pip-cache"
$py = Join-Path $pyDir "python.exe"
Write-Host "Bootstrapping pip + lean deps ..."
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile (Join-Path $Stage "get-pip.py") -UseBasicParsing
& $py (Join-Path $Stage "get-pip.py") --no-warn-script-location -q
if ($LASTEXITCODE -ne 0) { throw "Embedded Python pip bootstrap failed." }
# setuptools+wheel must be present before dependency installation so pywebview's build backend
# is available in the embedded Python. pywebview+pythonnet drive the native WebView2 app window
# (vool_window.py). Still no heavy ML training stack.
& $py -m pip install --no-warn-script-location -q `
    setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Embedded Python setuptools/wheel bootstrap failed." }
& $py -m pip install --no-build-isolation --no-warn-script-location -q `
    "idna==3.18" pydantic cryptography requests pynacl keyring psutil pyyaml `
    "pypdf==6.19.0" "reportlab==5.0.1" "markdown-it-py==4.2.0" `
    "starlette>=0.37,<2.0" "uvicorn>=0.30,<1.0" solders `
    "zstandard>=0.22.0" "xxhash>=3.4.0" `
    pywebview pythonnet "xlrd==2.0.1"
if ($LASTEXITCODE -ne 0) { throw "Embedded Python runtime dependency installation failed." }
& $py -m pip check
if ($LASTEXITCODE -ne 0) { throw "Embedded Python dependency check failed." }
Remove-Item (Join-Path $Stage "get-pip.py") -Force -ErrorAction SilentlyContinue

# --- 3. VOOL source ----------------------------------------------------------------------
$appDir = Join-Path $Stage "app"
New-Item -ItemType Directory -Force $appDir | Out-Null
# All top-level runtime packages the server imports (verified by running the staged bundle),
# plus config data. Excludes tests and dev-only trees to keep the bundle lean.
$runtimeRoots = @("apps", "core", "adapters", "storage", "network", "relay", "retrieval", "sandbox", "tools", "channels", "ops", "installer", "config")
$groundingSources = @("README.md", "docs\SYSTEM_SPINE.md", "docs\STATUS.md", "docs\VOOL_DESKTOP_BLUEPRINT.md")
if ($BuildMode -eq "Internal") {
  $internalSourceFiles = @(Copy-InternalBundleSource -RepoRoot $RepoRoot -AppDir $appDir -Paths ($runtimeRoots + $groundingSources))
} else {
  foreach ($d in $runtimeRoots) {
    $src = Join-Path $SourceRoot $d
    if (Test-Path $src) { Copy-Item $src (Join-Path $appDir $d) -Recurse -Force }
  }
}
# Canonical project grounding reads only this small allowlist. Ship the same sources in the
# installed bundle so a release build has the grounding that a source checkout has.
foreach ($relativePath in $groundingSources) {
  $src = Join-Path $SourceRoot $relativePath
  if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
    throw "Canonical grounding source missing: $relativePath"
  }
  $dest = Join-Path $appDir $relativePath
  New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
  Copy-Item -LiteralPath $src -Destination $dest -Force
}
# drop bytecode caches to keep the bundle clean
Get-ChildItem $appDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
foreach ($buildOutput in @(
  "installer\windows_host\bin", "installer\windows_host\obj",
  "installer\windows_broker\bin", "installer\windows_broker\obj",
  "installer\windows_host\tests\bin", "installer\windows_host\tests\obj",
  "installer\windows_broker\tests\bin", "installer\windows_broker\tests\obj"
)) {
  $buildOutputPath = Join-Path $appDir $buildOutput
  if (Test-Path -LiteralPath $buildOutputPath) {
    Remove-Item -LiteralPath $buildOutputPath -Recurse -Force
  }
}

# A bundle has no .git directory at runtime. Stamp the exact source revision into the staged app
# so /healthz, /api/runtime/version, and the chat footer can distinguish this installer from an
# older build. The shared helper must read the commit from the checkout itself. If Windows Git
# cannot inspect the source checkout (for example, a WSL UNC worktree), build from a clean native
# checkout instead of accepting caller-supplied provenance.
$buildSourcePath = Join-Path $appDir "config\build-source.json"
if ($BuildMode -eq "Release") {
  $buildSource = [ordered]@{
    schema = "vool.build_source.v1"
    source_kind = "git"
    commit = $commitFull.Substring(0, 12)
    commit_full = $commitFull
    dirty_state = $false
  }
  $buildSource | ConvertTo-Json | Set-Content -LiteralPath $buildSourcePath -Encoding utf8
  $buildSource = Get-Content -Raw $buildSourcePath | ConvertFrom-Json
} else {
  Push-Location $RepoRoot
  try {
    & $py (Join-Path $RepoRoot "installer\stamp_build_source.py") --root $RepoRoot --out $buildSourcePath
    if ($LASTEXITCODE -ne 0) { throw "Failed to stamp bundle source provenance." }
  }
  finally {
    Pop-Location
  }
  $buildSource = Get-Content -Raw $buildSourcePath | ConvertFrom-Json
}
if ([string]$buildSource.source_kind -ne "git" -or [string]$buildSource.commit_full -notmatch '^[0-9a-f]{40}$') {
  throw "Bundle source provenance is not an exact Git commit. Build from a native Git checkout."
}
if ($BuildMode -eq "Release" -and [bool]$buildSource.dirty_state) {
  throw "Bundle source checkout is dirty. Commit or remove local changes before building."
}
if ($BuildMode -eq "Internal") {
  Assert-InternalBundlePayload -RepoRoot $RepoRoot -AppDir $appDir -SourceFiles $internalSourceFiles
}
$appVersion = (& $py -c "import sys; sys.path.insert(0, r'$appDir'); from core.app_version import VOOL_VERSION; print(VOOL_VERSION)").Trim()
if (-not $appVersion) { throw "Failed to resolve the staged VOOL version." }

# --- 4. Ollama + launcher -----------------------------------------------------------------
if (-not (Test-Path -LiteralPath $WebView2Installer -PathType Leaf)) {
  throw "The offline x64 WebView2 Runtime installer is required for a self-contained Windows bundle."
}
if ($WebView2Sha256 -notmatch '^[0-9a-fA-F]{64}$') {
  throw "Pass the pinned SHA-256 of the offline x64 WebView2 Runtime installer."
}
$webView2ActualSha256 = (Get-FileHash -LiteralPath $WebView2Installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($webView2ActualSha256 -ne $WebView2Sha256.ToLowerInvariant()) {
  throw "WebView2 Runtime installer SHA-256 did not match the pinned build input."
}
$webView2Signature = Get-AuthenticodeSignature -LiteralPath $WebView2Installer
if ($webView2Signature.Status -ne 'Valid' -or
    -not $webView2Signature.SignerCertificate -or
    $webView2Signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
  throw "WebView2 Runtime installer is not validly signed by Microsoft Corporation."
}
$webView2Dir = Join-Path $Stage "webview2"
New-Item -ItemType Directory -Force $webView2Dir | Out-Null
Copy-Item -LiteralPath $WebView2Installer -Destination (Join-Path $webView2Dir "MicrosoftEdgeWebView2RuntimeInstallerX64.exe") -Force

New-Item -ItemType Directory -Force (Join-Path $Stage "ollama") | Out-Null
Copy-Item $OllamaExe (Join-Path $Stage "ollama\ollama.exe") -Force
$ollamaInstallRoot = Split-Path -Parent $OllamaExe
$ollamaSupportSource = Join-Path $ollamaInstallRoot "lib\ollama"
$ollamaRunner = Join-Path $ollamaSupportSource "llama-server.exe"
if (-not (Test-Path -LiteralPath $ollamaSupportSource -PathType Container)) {
  throw "Ollama runtime support directory not found: $ollamaSupportSource. Use a complete official Ollama installation."
}
if (-not (Test-Path -LiteralPath $ollamaRunner -PathType Leaf)) {
  throw "Ollama runner missing from the installation: $ollamaRunner. Install or repair Ollama before building the bundle."
}
Copy-Item $ollamaSupportSource (Join-Path $Stage "ollama\lib\ollama") -Recurse -Force
New-Item -ItemType Directory -Force (Join-Path $Stage "models") | Out-Null
Copy-Item (Join-Path $PSScriptRoot "vool-launch.cmd") (Join-Path $Stage "VOOL.cmd") -Force
Copy-Item (Join-Path $PSScriptRoot "vool.vbs") (Join-Path $Stage "vool.vbs") -Force
Copy-Item (Join-Path $PSScriptRoot "vool_window.py") (Join-Path $Stage "vool_window.py") -Force
Copy-Item (Join-Path $PSScriptRoot "bundle_supervisor.py") (Join-Path $Stage "bundle_supervisor.py") -Force
Copy-Item (Join-Path $PSScriptRoot "migrate_install_state.py") (Join-Path $Stage "migrate_install_state.py") -Force
Copy-Item (Join-Path $PSScriptRoot "vool_protocol_handler.py") (Join-Path $Stage "vool_protocol_handler.py") -Force

# Publish the native user-facing host and fail-closed execution broker into the installed payload.
$nativeDir = Join-Path $Stage "native"
$hostDir = Join-Path $nativeDir "host"
$brokerDir = Join-Path $nativeDir "broker"
New-Item -ItemType Directory -Force $hostDir, $brokerDir | Out-Null
Push-Location $SourceRoot
try {
  & $DotNetExe publish (Join-Path $SourceRoot "installer\windows_host\VOOL.WindowsHost.csproj") `
    --configuration Release --runtime win-x64 --self-contained true --output $hostDir -p:RestoreLockedMode=true
  if ($LASTEXITCODE -ne 0) { throw "Native Windows host publish failed." }
  & $DotNetExe publish (Join-Path $SourceRoot "installer\windows_broker\Vool.WindowsBroker.csproj") `
    --configuration Release --runtime win-x64 --self-contained true --output $brokerDir
  if ($LASTEXITCODE -ne 0) { throw "Native Windows broker publish failed." }
}
finally {
  Pop-Location
}
if (-not (Test-Path -LiteralPath (Join-Path $hostDir "VOOL.exe"))) { throw "Published VOOL.exe missing." }
if (-not (Test-Path -LiteralPath (Join-Path $brokerDir "vool-broker.exe"))) { throw "Published broker missing." }

# The bundle manifest is the single model-selection source for the launcher, runtime, model
# store, and doctor. It is deliberately written before an optional model pull so a failed pull
# cannot change the model the installed bundle claims to ship.
$manifest = [ordered]@{
  schema = "vool.bundle_manifest.v1"
  app_version = $appVersion
  source_commit = [string]$buildSource.commit
  source_commit_full = [string]$buildSource.commit_full
  source_dirty_state = [bool]$buildSource.dirty_state
  release = ($BuildMode -eq "Release")
  build_mode = $BuildMode.ToLowerInvariant()
  webview2_installer_sha256 = $webView2ActualSha256
  selected_model = $Model
  model_store = "%LOCALAPPDATA%\VOOL\models"
  api_url = "http://127.0.0.1:11435"
  ollama_url = "http://127.0.0.1:11434"
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $Stage "bundle_manifest.json") -Encoding utf8

if ($releaseArchiveRoot) {
  Remove-Item -LiteralPath $releaseArchiveRoot -Recurse -Force
}

# --- 5. optional model embed (offline bundle) ---------------------------------------------
if ($IncludeModel) {
  Write-Host "Embedding model $Model into the bundle (offline mode) ..."
  $env:OLLAMA_MODELS = Join-Path $Stage "models"
  & $OllamaExe pull $Model
}

# Python bytecode caches and PDB symbol files are build artifacts, not runtime
# requirements. Remove them after all staging steps so the installed payload does
# not retain build-root paths or optional native debug symbols.
Get-ChildItem -LiteralPath $Stage -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Extension -in @(".pyc", ".pdb") } |
  Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $Stage -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if ($BuildMode -eq "Internal") {
  Assert-InternalBundlePayload -RepoRoot $RepoRoot -AppDir $appDir -SourceFiles $internalSourceFiles
}
# Hash after final bytecode/symbol removal, then create the installer-only bootstrap.
Write-PythonUpgradeInputs -Stage $Stage -PythonDir $pyDir -PythonExe $py `
  -Helper (Join-Path $PSScriptRoot "upgrade_python_payload.py")
$sz = (Get-ChildItem $Stage -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Bundle staged at {0} -- {1:N0} MB" -f $Stage, ($sz/1MB))
