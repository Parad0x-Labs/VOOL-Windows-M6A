#requires -Version 5.1
#requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$')]
    [string]$RunId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ruleGroup = "VOOL-Lifecycle-$RunId"
$ruleMarker = "VOOL lifecycle firewall helper v1; run=$RunId"
$displayPrefix = "VOOL lifecycle outbound block [$RunId] "

$rules = @(Get-NetFirewallRule -Group $ruleGroup -ErrorAction SilentlyContinue | Where-Object {
    $displayName = [string]$_.DisplayName
    $displayName.StartsWith($displayPrefix, [System.StringComparison]::Ordinal) -and
    $_.Description -eq $ruleMarker -and
    $_.Direction -eq 'Outbound' -and
    $_.Action -eq 'Block'
})

$removedNames = New-Object System.Collections.Generic.List[string]
foreach ($rule in $rules) {
    Remove-NetFirewallRule -Name $rule.Name -ErrorAction Stop
    $removedNames.Add($rule.Name)
}

[pscustomobject]@{
    run_id = $RunId
    rule_group = $ruleGroup
    removed_rule_names = @($removedNames)
}
