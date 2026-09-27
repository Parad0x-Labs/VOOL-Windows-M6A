#requires -Version 5.1
#requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BundleRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ruleGroup = "VOOL-Lifecycle-$RunId"
$ruleMarker = "VOOL lifecycle firewall helper v1; run=$RunId"
$nonLoopbackRemoteAddresses = @(
    '0.0.0.0-126.255.255.255',
    '128.0.0.0-255.255.255.255',
    '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'
)

# These are the only VOOL-owned executable locations in the normal Windows bundle path.
# The broker is deliberately absent: the normal diagnostic launch does not start it.
$relativePrograms = @(
    'native\VOOL.exe',
    'python\python.exe',
    'python\pythonw.exe',
    'ollama\ollama.exe',
    'ollama\lib\ollama\llama-server.exe'
)

function Get-PathHash([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Path.ToLowerInvariant())
        $digest = $sha.ComputeHash($bytes)
        return (-join ($digest | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
    }
    finally {
        $sha.Dispose()
    }
}

$rootItem = Get-Item -LiteralPath $BundleRoot -Force
if (-not $rootItem.PSIsContainer) {
    throw "BundleRoot must be an existing directory: $BundleRoot"
}
$root = $rootItem.FullName.TrimEnd('\')
$rootPrefix = "$root\"

$programs = New-Object System.Collections.Generic.List[string]
$missing = New-Object System.Collections.Generic.List[string]
foreach ($relativeProgram in $relativePrograms) {
    $program = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($root, $relativeProgram))
    if (-not $program.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing executable outside BundleRoot: $program"
    }
    if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {
        $missing.Add($program)
        continue
    }
    $programs.Add(([System.IO.FileInfo](Get-Item -LiteralPath $program -Force)).FullName)
}
if ($missing.Count -gt 0) {
    throw "Refusing partial firewall setup; missing expected VOOL bundle executables: $($missing -join '; ')"
}

$existingGroup = @(Get-NetFirewallRule -Group $ruleGroup -ErrorAction SilentlyContinue)
if ($existingGroup.Count -gt 0) {
    throw "Firewall rule group already exists; use a new lifecycle RunId: $ruleGroup"
}

$createdNames = New-Object System.Collections.Generic.List[string]
try {
    foreach ($program in $programs) {
        $pathHash = Get-PathHash $program
        $ruleName = "$ruleGroup-$pathHash"
        if ($null -ne (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
            throw "Firewall rule already exists: $ruleName"
        }

        New-NetFirewallRule `
            -Name $ruleName `
            -DisplayName "VOOL lifecycle outbound block [$RunId] $pathHash" `
            -Group $ruleGroup `
            -Description $ruleMarker `
            -Direction Outbound `
            -Action Block `
            -Program $program `
            -Profile Any `
            -Protocol Any `
            -LocalAddress Any `
            -RemoteAddress $nonLoopbackRemoteAddresses `
            -Enabled True `
            -ErrorAction Stop | Out-Null
        $createdNames.Add($ruleName)
    }
}
catch {
    foreach ($createdName in $createdNames) {
        Remove-NetFirewallRule -Name $createdName -ErrorAction SilentlyContinue
    }
    throw
}

[pscustomobject]@{
    run_id = $RunId
    rule_group = $ruleGroup
    direction = 'Outbound'
    action = 'Block'
    loopback_preserved = $true
    programs = @($programs)
    rule_names = @($createdNames)
}
