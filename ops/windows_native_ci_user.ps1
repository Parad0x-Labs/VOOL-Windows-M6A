param(
    [ValidateSet('Parent', 'Child')][string]$Mode = 'Parent',
    [string]$Root,
    [string]$ExpectedSid
)

# Fixed CI entry point, not a general credentialed command runner.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-HostedRunner {
    if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or
        $env:RUNNER_OS -ne 'Windows' -or $env:GITHUB_JOB -ne 'gauntlet' -or
        $env:GITHUB_REPOSITORY -ne 'Parad0x-Labs/VOOL-Windows-M6A' -or
        $env:GITHUB_RUN_ID -notmatch '^[1-9][0-9]*$') {
        throw 'Disposable Windows gauntlet runner required; account operations refused.'
    }
}

function Assert-InputManifest([string]$InputRoot, $Manifest) {
    $files = @(Get-ChildItem -LiteralPath $InputRoot -File -Recurse -Force)
    if ($files.Count -ne @($Manifest).Count) { throw 'Payload inventory differs.' }
    foreach ($entry in $Manifest) {
        if ($entry.path -notmatch '^(tests|broker|entry)/[A-Za-z0-9_.-]+$') {
            throw 'Unexpected payload path.'
        }
        $file = Get-Item -LiteralPath (Join-Path $InputRoot $entry.path)
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $entry.sha256) {
            throw ('Payload hash or regular-file check failed: ' + $entry.path)
        }
    }
}

function Assert-UserContext($Context, [string]$Sid, [string]$Profile) {
    if ($Context.user -ne $Sid -or $Context.owner -ne $Sid -or $Context.admin -or
        $Context.integrity -ne 'S-1-16-8192' -or $Context.impersonation -ne 'None' -or
        -not $Profile -or $Context.profile -ne $Profile -or
        $Context.localAppData -ne (Join-Path $Profile 'AppData\Local') -or
        -not $Context.temp.StartsWith($Context.localAppData + '\Temp\', [StringComparison]::OrdinalIgnoreCase) -or
        $Context.tmp -ne $Context.temp) {
        throw 'Standard-user identity, integrity or profile binding refused.'
    }
}

function Assert-AccountCleanup([bool]$CleanupObserved, [string]$CreatedSid, $CurrentAccount) {
    if (-not $CleanupObserved -or -not $CurrentAccount -or
        $CurrentAccount.SID.Value -ne $CreatedSid) {
        throw 'Account removal refused: child cleanup or exact SID binding unproven.'
    }
}

function Assert-NativeCompletion($Result) {
    if (-not $Result.Completed -or $Result.TimedOut -or
        -not $Result.CleanupObserved -or $null -eq $Result.ExitCode) {
        throw 'Native launch, deadline or cleanup refused; see native receipt.'
    }
}

function New-TestCredential {
    $secret = [Security.SecureString]::new()
    try {
        foreach ($char in 'aA0!'.ToCharArray()) { $secret.AppendChar($char) }
        $random = New-Object byte[] 48
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($random) } finally { $rng.Dispose() }
        foreach ($value in $random) { $secret.AppendChar([char](33 + ($value % 94))) }
        [Array]::Clear($random, 0, $random.Length)
        $secret.MakeReadOnly()
        return $secret
    } catch { $secret.Dispose(); throw }
}

function Initialize-NativeEntry {
    if ('VoolCiEntry' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;
using System.Text;
using System.Threading;

public sealed class VoolCiResult {
    public string Stage = "prepare";
    public int? NativeError;
    public uint? ExitCode;
    public uint ProcessId;
    public int Session;
    public long CreationTime;
    public bool TimedOut;
    public bool CleanupObserved = true;
    public bool Completed;
}

public static class VoolCiEntry {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct Startup {
        public uint cb; public string reserved, desktop, title;
        public uint x,y,xsize,ysize,xchars,ychars,fill,flags;
        public ushort show, reservedSize; public IntPtr reserved2, stdin, stdout, stderr;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
        public IntPtr process, thread; public uint pid, tid;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long user, kernel, periodUser, periodKernel;
        public uint faults, total, active, terminated;
    }
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcessWithLogonW(string user, string domain, IntPtr password,
        uint logon, string application, StringBuilder command, uint flags, IntPtr environment,
        string directory, ref Startup startup, out ProcessInfo info);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer, uint size, out uint needed);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr buffer, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, out Accounting info, uint size, IntPtr needed);
    [DllImport("kernel32.dll", EntryPoint="QueryInformationJobObject", SetLastError=true)]
    static extern bool QueryJobLimits(IntPtr job, int kind, IntPtr info, uint size, IntPtr needed);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);

    static void Require(bool ok, string stage) {
        if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error(), stage);
    }
    static bool EmptyJob(IntPtr job) {
        for (int i=0; i<200; i++) {
            Accounting value;
            Require(QueryInformationJobObject(job, 1, out value, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero), "query-job");
            if (value.active==0) return true;
            Thread.Sleep(50);
        }
        return false;
    }
    static void ConfigureOwnedJob(IntPtr job) {
        // win-x64 JOBOBJECT_EXTENDED_LIMIT_INFORMATION: 144 bytes; flags at offset 16.
        IntPtr limits=Marshal.AllocHGlobal(144);
        try {
            Marshal.Copy(new byte[144], 0, limits, 144);
            Marshal.WriteInt32(limits, 16, 0x2000); // KILL_ON_JOB_CLOSE, no breakaway permission.
            Require(SetInformationJobObject(job, 9, limits, 144), "set-job-limits");
        } finally { Marshal.FreeHGlobal(limits); }
    }
    // Native fixture only: exercise the SAME setup and query/close an owned EMPTY job.
    // No process, token, account, credential or logon is created by this fixture.
    public static uint ProbeEmptyJobPolicy() {
        if (IntPtr.Size!=8) throw new InvalidOperationException("win-x64 required.");
        IntPtr job=CreateJobObjectW(IntPtr.Zero, null);
        Require(job!=IntPtr.Zero, "fixture-create-job");
        try {
            ConfigureOwnedJob(job);
            IntPtr limits=Marshal.AllocHGlobal(144);
            try {
                Require(QueryJobLimits(job, 9, limits, 144, IntPtr.Zero), "fixture-query-limits");
                uint flags=unchecked((uint)Marshal.ReadInt32(limits, 16));
                if (flags!=0x2000 || !EmptyJob(job)) throw new InvalidOperationException("Empty-job policy differs.");
                return flags;
            } finally { Marshal.FreeHGlobal(limits); }
        } finally { Require(CloseHandle(job), "fixture-close-job"); }
    }
    public static string Integrity(IntPtr token) {
        uint needed;
        bool probe = GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed);
        if (probe || Marshal.GetLastWin32Error()!=122 || needed< IntPtr.Size || needed>65536)
            throw new InvalidOperationException("Integrity size query refused.");
        IntPtr buffer = Marshal.AllocHGlobal((int)needed);
        try {
            Require(GetTokenInformation(token, 25, buffer, needed, out needed), "integrity-query");
            return new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value;
        } finally { Marshal.FreeHGlobal(buffer); }
    }
    // One fixed profile-loaded launch. No inherited environment, std handles or secret argv.
    // Desktop permissions are NOT changed; an unavailable desktop remains a launch failure.
    public static VoolCiResult Run(string root, string username, string sid, SecureString password) {
        VoolCiResult result = new VoolCiResult();
        ProcessInfo info = new ProcessInfo();
        IntPtr job = IntPtr.Zero, secret = IntPtr.Zero, token = IntPtr.Zero;
        bool assigned = false;
        try {
            if (Environment.GetEnvironmentVariable("GITHUB_ACTIONS")!="true" ||
                Environment.GetEnvironmentVariable("RUNNER_ENVIRONMENT")!="github-hosted" ||
                Environment.GetEnvironmentVariable("GITHUB_JOB")!="gauntlet")
                throw new InvalidOperationException("Hosted runner required for logon.");
            if (IntPtr.Size!=8) throw new InvalidOperationException("win-x64 required.");
            result.Stage="create-job";
            job=CreateJobObjectW(IntPtr.Zero, null); Require(job!=IntPtr.Zero, result.Stage);
            ConfigureOwnedJob(job);
            string image=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe");
            string script=Path.Combine(root, "payload", "entry", "windows_native_ci_user.ps1");
            StringBuilder command=new StringBuilder("\""+image+"\" -NoLogo -NoProfile -NonInteractive -File \""+script+"\" -Mode Child -Root \""+root+"\" -ExpectedSid "+sid);
            Startup start=new Startup(); start.cb=(uint)Marshal.SizeOf(typeof(Startup));
            start.flags=1; start.show=0;
            secret=Marshal.SecureStringToGlobalAllocUnicode(password);
            result.Stage="create-profile-process";
            try {
                Require(CreateProcessWithLogonW(username, ".", secret, 1, image, command, 0xC,
                    IntPtr.Zero, root, ref start, out info), result.Stage); // SUSPENDED | DETACHED
            } finally { Marshal.ZeroFreeGlobalAllocUnicode(secret); secret=IntPtr.Zero; }
            result.CleanupObserved=false; result.ProcessId=info.pid;
            long exit,kernel,user;
            Require(GetProcessTimes(info.process, out result.CreationTime, out exit, out kernel, out user), "creation-identity");
            Require(OpenProcessToken(info.process, 8, out token), "child-token");
            uint sessionBytes;
            IntPtr sessionBuffer=Marshal.AllocHGlobal(4);
            try {
                Require(GetTokenInformation(token, 12, sessionBuffer, 4, out sessionBytes), "child-session");
                if (sessionBytes!=4) throw new InvalidOperationException("Session result size refused.");
                result.Session=Marshal.ReadInt32(sessionBuffer);
                using (System.Diagnostics.Process parent=System.Diagnostics.Process.GetCurrentProcess()) {
                    if (result.Session!=parent.SessionId) throw new InvalidOperationException("Child session binding refused.");
                }
            } finally { Marshal.FreeHGlobal(sessionBuffer); }
            using (WindowsIdentity identity=new WindowsIdentity(token)) {
                if (identity.User.Value!=sid || identity.Owner.Value!=sid ||
                    new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator) || Integrity(token)!="S-1-16-8192")
                    throw new InvalidOperationException("Created child token refused.");
            }
            Require(AssignProcessToJobObject(job, info.process), "assign-job"); assigned=true;
            result.Stage="resume";
            uint previous=ResumeThread(info.thread);
            if (previous!=1) throw new InvalidOperationException("Single suspended resume refused.");
            result.Stage="wait-suite";
            uint wait=WaitForSingleObject(info.process, 600000);
            if (wait==258) { result.TimedOut=true; throw new TimeoutException("Native suite deadline."); }
            Require(wait==0, "wait-process");
            uint code; Require(GetExitCodeProcess(info.process, out code), "exit-code"); result.ExitCode=code;
            result.Stage="observe-empty-job";
            if (!EmptyJob(job)) throw new TimeoutException("Descendant cleanup unobserved.");
            result.CleanupObserved=true; result.Completed=true; result.Stage="completed";
        } catch (Win32Exception e) { result.Stage=e.Message; result.NativeError=e.NativeErrorCode;
        } catch (Exception e) { result.Stage=result.Stage+": "+e.GetType().Name;
        } finally {
            if (secret!=IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
            if (info.process!=IntPtr.Zero && !result.CleanupObserved) {
                try {
                    Require(assigned ? TerminateJobObject(job, 0xC000013A) : TerminateProcess(info.process, 0xC000013A), "terminate-owned-child");
                    Require(WaitForSingleObject(info.process, 10000)==0, "observe-termination");
                    result.CleanupObserved=!assigned || EmptyJob(job);
                } catch (Win32Exception e) { result.NativeError=e.NativeErrorCode; result.Stage+="; cleanup: "+e.Message;
                } catch (Exception e) { result.Stage+="; cleanup: "+e.GetType().Name; }
            }
            foreach (IntPtr handle in new IntPtr[]{ token, info.thread, info.process, job }) {
                if (handle!=IntPtr.Zero && !CloseHandle(handle)) {
                    result.NativeError=Marshal.GetLastWin32Error(); result.CleanupObserved=false;
                    result.Completed=false; result.Stage+="; close-handle refused";
                }
            }
        }
        return result;
    }
}
'@
}

function Invoke-ChildSuite {
    $report = [ordered]@{ stage = 'context'; exitCode = $null; context = $null; outputEncoding = 'utf-16le-bom'; error = $null }
    $code = 1
    try {
        if ($Root -notmatch '^[A-Za-z]:\\Users\\Public\\vool-native-ci-[a-f0-9]{32}$' -or
            $ExpectedSid -notmatch '^S-1-5-21-[0-9-]+$') { throw 'Fixed child binding refused.' }
        Initialize-NativeEntry
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        try {
            $profile = [Environment]::GetFolderPath('UserProfile')
            $local = [Environment]::GetFolderPath('LocalApplicationData')
            $binding = (Get-ItemProperty -LiteralPath ('HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $ExpectedSid)).ProfileImagePath
            $env:USERPROFILE = $profile
            $env:LOCALAPPDATA = $local
            $env:TEMP = Join-Path $local ('Temp\vool-native-ci-' + (Split-Path $Root -Leaf).Substring(15))
            $env:TMP = $env:TEMP
            $context = [ordered]@{
                user = $identity.User.Value; owner = $identity.Owner.Value
                admin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
                integrity = [VoolCiEntry]::Integrity($identity.Token)
                impersonation = $identity.ImpersonationLevel.ToString()
                profile = $profile; localAppData = $local; temp = $env:TEMP; tmp = $env:TMP
                session = [Diagnostics.Process]::GetCurrentProcess().SessionId
            }
            $report.context = $context
            Assert-UserContext $context $ExpectedSid $binding
        } finally { $identity.Dispose() }
        New-Item -ItemType Directory -Path $env:TEMP -ErrorAction Stop | Out-Null
        if ((Get-Acl -LiteralPath $env:TEMP).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $ExpectedSid) {
            throw 'New test TEMP owner refused.'
        }
        $manifest = @(Get-Content -LiteralPath (Join-Path $Root 'input-manifest.json') -Raw | ConvertFrom-Json)
        Assert-InputManifest (Join-Path $Root 'payload') $manifest
        $report.stage = 'native-suite'
        $tests = Join-Path $Root 'payload\tests\vool-broker-tests.exe'
        $broker = Join-Path $Root 'payload\broker\vool-broker.exe'
        $nativePreference = $ErrorActionPreference
        try {
            # Framework PowerShell reports redirected stderr as error records. Collect every
            # case instead of terminating on the first stderr line; retain the real exit code.
            $ErrorActionPreference = 'Continue'
            & $tests $broker 2>&1 | Tee-Object -FilePath (Join-Path $Root 'output\broker-output.log') -ErrorAction Stop
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $nativePreference }
        $report.exitCode = $code
        Assert-InputManifest (Join-Path $Root 'payload') $manifest
        $report.stage = 'completed'
    } catch {
        $report.error = $_.Exception.Message
        $code = 1
    } finally {
        $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Root 'output\child-result.json') -Encoding UTF8
    }
    exit $code
}

function Invoke-HostedSuite {
    Assert-HostedRunner # Before any account, logon, staging or permission mutation.
    $repo = Split-Path $PSScriptRoot -Parent
    $logs = Join-Path $repo '.windows-broker-logs'
    $Root = Join-Path ($env:SystemDrive + '\Users\Public') ('vool-native-ci-' + [Guid]::NewGuid().ToString('N'))
    $name = 'voolci_' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
    $account = $null
    $native = $null
    $launchAttempted = $false
    $secret = $null
    $receipt = [ordered]@{ run = $env:GITHUB_RUN_ID; attempt = $env:GITHUB_RUN_ATTEMPT; sourceCommit = $env:GITHUB_SHA; account = $name; sid = $null; groups = $null; root = $Root; payloadDacl = $null; outputDacl = $null; stage = 'inputs'; native = $null; accountRemoved = $false; error = $null }
    $code = 1
    try {
        $inputs = @()
        $tests = Join-Path $repo 'installer\windows_broker\tests\bin\Release\net10.0-windows10.0.17763.0\win-x64'
        foreach ($required in @('vool-broker-tests.exe', 'vool-broker-tests.dll', 'vool-broker-tests.runtimeconfig.json', 'vool-broker-tests.deps.json')) {
            Get-Item -LiteralPath (Join-Path $tests $required) -ErrorAction Stop | Out-Null
        }
        if (@(Get-ChildItem -LiteralPath $tests -Directory -Force).Count -ne 0) { throw 'Unexpected runtime subdirectory.' }
        foreach ($file in Get-ChildItem -LiteralPath $tests -File -Force) { $inputs += @{ source = $file.FullName; path = 'tests/' + $file.Name } }
        $inputs += @{ source = Join-Path $repo 'installer\windows_broker\bin\Release\net10.0-windows10.0.17763.0\win-x64\publish\vool-broker.exe'; path = 'broker/vool-broker.exe' }
        $inputs += @{ source = $PSCommandPath; path = 'entry/windows_native_ci_user.ps1' }
        foreach ($asset in $inputs) {
            $file = Get-Item -LiteralPath $asset.source
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse input refused.' }
            $asset.sha256 = (Get-FileHash -LiteralPath $file.FullName).Hash
        }
        Initialize-NativeEntry
        if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) { throw 'Account name collision.' }
        $secret = New-TestCredential
        $receipt.stage = 'create-account'
        $account = New-LocalUser -Name $name -Password $secret -Description ('VOOL disposable gauntlet run ' + $env:GITHUB_RUN_ID)
        $sid = $account.SID
        $receipt.sid = $sid.Value
        $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
        if (-not (@(Get-LocalGroupMember -SID $users | ForEach-Object { $_.SID.Value }) -contains $sid.Value)) { Add-LocalGroupMember -SID $users -Member $account }
        $groups = @(Get-LocalGroup | Where-Object { @(Get-LocalGroupMember -Group $_ | ForEach-Object { $_.SID.Value }) -contains $sid.Value })
        if ($groups.Count -ne 1 -or $groups[0].SID.Value -ne $users.Value) { throw 'Users-only membership refused.' }
        $receipt.groups = @($groups | ForEach-Object { $_.SID.Value })
        $receipt.stage = 'stage-payload'
        New-Item -ItemType Directory -Path $Root -ErrorAction Stop | Out-Null
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($grant in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @($sid.Value, 'ReadAndExecute'))) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($grant[0]), $grant[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $Root -AclObject $acl
        foreach ($directory in @('payload\tests', 'payload\broker', 'payload\entry', 'output')) { New-Item -ItemType Directory -Path (Join-Path $Root $directory) -ErrorAction Stop | Out-Null }
        $output = Join-Path $Root 'output'
        $outputAcl = Get-Acl -LiteralPath $output
        $outputAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
        Set-Acl -LiteralPath $output -AclObject $outputAcl
        $receipt.payloadDacl = (Get-Acl -LiteralPath $Root).Sddl
        $receipt.outputDacl = (Get-Acl -LiteralPath $output).Sddl
        foreach ($asset in $inputs) { Copy-Item -LiteralPath $asset.source -Destination (Join-Path $Root ('payload\' + $asset.path)) -ErrorAction Stop }
        $manifest = @($inputs | ForEach-Object { @{ path = $_.path; sha256 = $_.sha256 } })
        $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'input-manifest.json') -Encoding UTF8
        Assert-InputManifest (Join-Path $Root 'payload') $manifest
        $receipt.stage = 'fixed-logon'
        $launchAttempted = $true
        $native = [VoolCiEntry]::Run($Root, $name, $sid.Value, $secret)
        $receipt.native = $native
        Assert-NativeCompletion $native
        $code = [int]$native.ExitCode
        $child = Get-Content -LiteralPath (Join-Path $output 'child-result.json') -Raw | ConvertFrom-Json
        if ($child.stage -ne 'completed' -or $child.exitCode -ne $code -or
            $child.context.session -ne $native.Session) { throw 'Child completion, exit or session evidence differs.' }
        Assert-InputManifest (Join-Path $Root 'payload') $manifest
        $receipt.stage = 'observed-completion'
    } catch {
        $receipt.error = $_.Exception.Message
        $code = 1
    } finally {
        if ($secret) { $secret.Dispose() }
        if ($account) {
            try {
                $observed = -not $launchAttempted -or ($null -ne $native -and $native.CleanupObserved)
                $current = Get-LocalUser -Name $name
                Assert-AccountCleanup $observed $account.SID.Value $current
                Remove-LocalUser -SID $account.SID
                if (Get-LocalUser -SID $account.SID -ErrorAction SilentlyContinue) { throw 'Account removal unconfirmed.' }
                $receipt.accountRemoved = $true
            } catch { $receipt.error = [string]$receipt.error + '; Account cleanup refused: ' + $_.Exception.Message; $code = 1 }
        }
        try {
            if (Test-Path -LiteralPath (Join-Path $Root 'output')) {
                Copy-Item -LiteralPath (Join-Path $Root 'output') -Destination (Join-Path $logs 'standard-user-output') -Recurse -ErrorAction Stop
            }
            if (Test-Path -LiteralPath (Join-Path $Root 'input-manifest.json')) { Copy-Item -LiteralPath (Join-Path $Root 'input-manifest.json') -Destination $logs }
        } catch { $receipt.error = [string]$receipt.error + '; Evidence copy refused: ' + $_.Exception.Message; $code = 1 }
        $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $logs 'standard-user-receipt.json') -Encoding UTF8
        $receipt | ConvertTo-Json -Depth 8 | Write-Output
    }
    exit $code
}

# Dot-sourcing exposes only pure guards/native compilation for local contract fixtures.
if ($MyInvocation.InvocationName -ne '.') {
    if ($Mode -eq 'Child') { Invoke-ChildSuite } else { Invoke-HostedSuite }
}
