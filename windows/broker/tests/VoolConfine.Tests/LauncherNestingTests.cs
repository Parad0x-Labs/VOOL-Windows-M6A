using VoolConfine.Core;
using Xunit;
using static VoolConfine.Tests.LauncherTestSupport;

namespace VoolConfine.Tests;

/// <summary>
/// A process inside the box must be able to start another one (a test runner starts workers, a build
/// starts a compiler). This runs the launcher's own job and child code in-process with a few settings
/// varied, so a failure names the setting that causes it.
/// </summary>
public class LauncherNestingTests
{
    static (int Code, string Detail) Nested(ConfineSandbox box, uint ui, string currentDirectory)
    {
        var package = Sids.EnsureProfile();
        var capabilities = Sids.CapabilitiesFor(Policy.Parse(System.Text.Json.JsonSerializer.Serialize(box.Policy())));
        using var job = ConfinementJob.Create(16, 1024, ui);
        var commandLine = LauncherCommandLine.Build([Cmd, "/c", $"{Cmd} /c exit 7"]);
        using var child = LauncherProcess.CreateSuspendedInJob(Cmd, commandLine, package, capabilities, job,
            Launcher.ChildEnvironment(box.Temp), currentDirectory);
        LauncherNative.ResumeThread(child.Thread);
        var waited = LauncherNative.WaitForSingleObject(child.Process, 30_000);
        LauncherNative.GetExitCodeProcess(child.Process, out var code);
        job.KillAll(1, TimeSpan.FromSeconds(5));
        return ((int)code, $"ui=0x{ui:X2} cwd={currentDirectory} wait={waited} exit={code}");
    }

    [Fact]
    public void A_child_in_the_default_job_can_start_a_grandchild()
    {
        using var box = new ConfineSandbox();
        var variants = new List<(int Code, string Detail)>
        {
            Nested(box, LauncherNative.JobObjectUiLimitAll, System32),
            Nested(box, 0, System32),
            Nested(box, LauncherNative.JobObjectUiLimitAll, box.Work),
            Nested(box, 0, box.Work),
        };
        // Each UI restriction on its own, to name the one that matters if any does.
        for (var bit = 1u; bit <= 0x80; bit <<= 1)
            variants.Add(Nested(box, bit, System32));
        var table = string.Join("; ", variants.Select(v => v.Detail));
        Assert.True(variants[0].Code == 7, $"the default job could not start a grandchild: {table}");
    }

    // From inside the box, CreateProcessW with a NULL or explicit directory, with and without handle
    // inheritance, before and after the child resets its own current directory, printed as
    // "case=OK<exit>" or "case=ERR<win32>" so a failure names the choice that matters.
    // PowerShell turns $null into "" for a string parameter, so a real NULL directory needs
    // [NullString]::Value (run 41906745205's "=ERR123" rows were "" and say nothing about NULL).
    const string Matrix = """
        $sig = @'
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] public static extern bool CreateProcessW(string app, System.Text.StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string dir, byte[] si, byte[] pi);
        [DllImport("kernel32.dll")] public static extern uint WaitForSingleObject(IntPtr h, uint ms);
        [DllImport("kernel32.dll")] public static extern bool GetExitCodeProcess(IntPtr h, out uint code);
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] public static extern bool SetCurrentDirectoryW(string dir);
        '@
        $k = Add-Type -MemberDefinition $sig -Name K -Namespace VoolConfineNest -PassThru
        $app = "$env:SystemRoot\System32\cmd.exe"
        $here = [Environment]::CurrentDirectory
        $out = @("CWD=$here", "TEMP=$env:TEMP")
        function Try-Create($label, $dir, $inherit) {
          $si = New-Object byte[] 104; $si[0] = 104
          $pi = New-Object byte[] 24
          $cmd = New-Object System.Text.StringBuilder 'cmd.exe /c exit 7'
          $ok = $k::CreateProcessW($app, $cmd, [IntPtr]::Zero, [IntPtr]::Zero, $inherit, 0, [IntPtr]::Zero, $dir, $si, $pi)
          $err = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
          if (-not $ok) { return "$label=ERR$err" }
          $h = [IntPtr][BitConverter]::ToInt64($pi, 0)
          [void]$k::WaitForSingleObject($h, 10000)
          $code = 0; [void]$k::GetExitCodeProcess($h, [ref]$code)
          return "$label=OK$code"
        }
        foreach ($inherit in $false, $true) {
          $out += Try-Create "null/inherit=$inherit" ([NullString]::Value) $inherit
          $out += Try-Create "here/inherit=$inherit" $here $inherit
        }
        $set = $k::SetCurrentDirectoryW($here)
        $out += "SETCWD=$set"
        foreach ($inherit in $false, $true) {
          $out += Try-Create "afterset-null/inherit=$inherit" ([NullString]::Value) $inherit
        }
        'MATRIX ' + ($out -join '; ')
        """;

    [Fact]
    public void Process_creation_from_inside_the_box_works_with_the_defaults()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, PowerShellArgs(Matrix.Replace("WORKROOT", box.Work)));
        Assert.True(Started(result), result.Stderr);
        Assert.True(result.Stdout.Contains("null/inherit=False=OK7"), $"exit {result.ExitCode}; {result.Stdout}; err: {result.Stderr}");
    }
}
