<#
Authenticode-sign release files: the one place a Windows signing route plugs in.

release-windows.yml runs this only when the repository variable WINDOWS_SIGNING_ROUTE is set, and
none is today: Parad0x Labs LLC does not qualify for Azure Trusted Signing yet, so releases ship
unsigned, with SHA256SUMS signed offline by the Ed25519 release key instead
(guides: Windows roadmap, track S, "Fallback signing route"). A route (an individual code-signing
certificate in a CA's cloud HSM, or Azure Trusted Signing) is added as one branch below. It must sign
each file in place and verify it with Get-AuthenticodeSignature; the workflow writes SHA256SUMS
after this step, so the sums cover the signed bytes.

Every route not implemented here fails, so setting the variable never ships an unsigned file that
claims to be signed.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Route,
    [Parameter(Mandatory = $true)][string[]]$Files
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "nothing to sign at $file" }
}
switch ($Route) {
    default { throw "Windows signing route '$Route' is not wired; nothing was signed." }
}
