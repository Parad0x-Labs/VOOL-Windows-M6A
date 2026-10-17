"""Independent NTFS assertions through the operating system, not the production ACL writer."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _powershell(path: Path, script: str):
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # The Windows PowerShell child must not import an incompatible module from a PowerShell 7 parent.
    environment = dict(os.environ, VOOL_TEST_ACL_PATH=str(path), PSModulePath=str(executable.parent / "Modules"))
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            env=environment, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def assert_owner_only(path: Path, *, directory: bool = False) -> None:
    result = _powershell(path, """
        $ErrorActionPreference='Stop'
        $acl=Get-Acl -LiteralPath $env:VOOL_TEST_ACL_PATH
        $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        $rules=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]))
        [pscustomobject]@{
            protected=$acl.AreAccessRulesProtected
            owner_matches_user=($owner -eq $sid)
            rules=@($rules | ForEach-Object {[pscustomobject]@{
                owner=($_.IdentityReference.Value -eq $sid)
                allow=($_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow)
                rights=[int]$_.FileSystemRights
                inheritance=[int]$_.InheritanceFlags
                propagation=[int]$_.PropagationFlags
                inherited=$_.IsInherited
            }})
        } | ConvertTo-Json -Depth 5 -Compress
    """)
    assert result["protected"], "The ledger DACL must block inherited non-owner grants"
    assert result["owner_matches_user"]
    assert len(result["rules"]) == 1, result
    rule = result["rules"][0]
    assert rule == {"owner": True, "allow": True, "rights": 0x1F01FF,
                    "inheritance": 3 if directory else 0, "propagation": 0, "inherited": False}, result


def grant_everyone_read(path: Path) -> None:
    """Broaden only a disposable test object to prove tightening and negative controls."""
    assert _powershell(path, """
        $ErrorActionPreference='Stop'
        $path=$env:VOOL_TEST_ACL_PATH
        $directory=[System.IO.Directory]::Exists($path)
        $section=[System.Security.AccessControl.AccessControlSections]::Access
        if($directory){$acl=[System.IO.Directory]::GetAccessControl($path,$section)}
        else{$acl=[System.IO.File]::GetAccessControl($path,$section)}
        $sid=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
        $rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'Read','Allow')
        $acl.AddAccessRule($rule)
        if($directory){[System.IO.Directory]::SetAccessControl($path,$acl)}
        else{[System.IO.File]::SetAccessControl($path,$acl)}
        $true | ConvertTo-Json -Compress
    """) is True
