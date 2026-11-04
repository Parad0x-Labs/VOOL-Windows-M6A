using System.Runtime.InteropServices;
using VoolConfine.Core;
using Xunit;

namespace VoolConfine.Tests;

/// <summary>The launcher's parts on their own: quoting, program rule, job limits, SIDs.</summary>
public class LauncherUnitTests
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern nint CommandLineToArgvW(string commandLine, out int count);

    [DllImport("kernel32.dll")]
    static extern nint LocalFree(nint memory);

    static string[] Split(string commandLine)
    {
        var argv = CommandLineToArgvW(commandLine, out var count);
        Assert.NotEqual(0, argv);
        try
        {
            var result = new string[count];
            for (var i = 0; i < count; i++) result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv, i * IntPtr.Size))!;
            return result;
        }
        finally { LocalFree(argv); }
    }

    [Theory]
    [InlineData("plain")]
    [InlineData("")]
    [InlineData("two words")]
    [InlineData("tab\there")]
    [InlineData("say \"hi\"")]
    [InlineData(@"C:\trailing\")]
    [InlineData(@"C:\trailing space\")]
    [InlineData(@"back\\\""quote")]
    [InlineData(@"\\server\share\x")]
    [InlineData("\"")]
    [InlineData(@"a\\b c\\")]
    [InlineData("%PATH% & calc ^ | <x> !y!")]
    public void Every_argument_comes_back_exactly_as_given(string argument)
    {
        string[] argv = [@"C:\Program Files\Tool\tool.exe", argument, "after"];
        Assert.Equal(argv, Split(LauncherCommandLine.Build(argv)));
    }

    [Theory]
    [InlineData("cmd.exe")]
    [InlineData(@"Windows\System32\cmd.exe")]
    [InlineData(@"\Windows\System32\cmd.exe")]
    [InlineData(@"C:\tools\npm.cmd")]
    [InlineData(@"C:\tools\build.bat")]
    [InlineData(@"C:\tools\BUILD.BAT")]
    [InlineData(@"C:\tools\run.ps1")]
    [InlineData(@"C:\tools\tool.exe.")]
    [InlineData(@"C:\tools\tool.exe ")]
    [InlineData(@"C:\tools\x:stream.exe")]
    [InlineData(@"C:\tools\.exe")]
    [InlineData(@"C:\tools\..\evil.exe")]
    [InlineData("C:\\tools\\a\"b.exe")]
    [InlineData(@"\\server\share\tool.exe")]
    [InlineData(@"\\?\C:\tools\tool.exe")]
    [InlineData("")]
    public void A_program_that_is_not_an_absolute_exe_is_refused(string program)
    {
        Assert.Throws<PolicyException>(() => LauncherCommandLine.Build([program]));
    }

    [Fact]
    public void An_absolute_exe_is_accepted_in_any_case()
    {
        Assert.Equal("\"C:\\Windows\\System32\\CMD.EXE\" /c", LauncherCommandLine.Build([@"C:\Windows\System32\CMD.EXE", "/c"]));
    }

    [Fact]
    public void The_environment_block_is_sorted_and_double_terminated()
    {
        var block = LauncherProcess.EnvironmentBlock(new Dictionary<string, string> { ["b"] = "2", ["A"] = "1", ["=C:"] = @"C:\" });
        Assert.Equal("=C:=C:\\\0A=1\0b=2\0\0", block);
        Assert.Throws<SetupException>(() => LauncherProcess.EnvironmentBlock(new Dictionary<string, string> { ["X"] = "a\0b" }));
    }

    [Fact]
    public void Temp_and_tmp_are_set_to_the_policy_temp_dir()
    {
        var environment = Launcher.ChildEnvironment(@"C:\work\repo\.sandbox-tmp");
        Assert.Equal(@"C:\work\repo\.sandbox-tmp", environment["TEMP"]);
        Assert.Equal(@"C:\work\repo\.sandbox-tmp", environment["tmp"]);
    }

    [Fact]
    public void The_job_reads_back_with_kill_on_close_and_no_breakaway()
    {
        using var job = ConfinementJob.Create(maxProcesses: 7, memoryMb: 300);
        var limits = job.ReadBack();
        Assert.Equal(ConfinementJob.RequiredFlags, limits.LimitFlags);
        Assert.NotEqual(0u, limits.LimitFlags & LauncherNative.JobObjectLimitKillOnJobClose);
        Assert.Equal(0u, limits.LimitFlags & (LauncherNative.JobObjectLimitBreakawayOk | LauncherNative.JobObjectLimitSilentBreakawayOk));
        Assert.Equal(7u, limits.ActiveProcessLimit);
        Assert.Equal(300UL * 1024 * 1024, limits.JobMemoryLimit);
        Assert.Equal(LauncherNative.JobObjectUiLimitAll, limits.UiRestrictions);
        Assert.Equal(0u, job.ActiveProcesses());
    }

    [Theory]
    [InlineData(0, 100)]
    [InlineData(5, 0)]
    public void A_job_with_a_non_positive_limit_is_refused(int processes, int memory)
    {
        Assert.Throws<SetupException>(() => ConfinementJob.Create(processes, memory));
    }

    [Fact]
    public void The_profile_and_capability_sids_have_the_appcontainer_forms()
    {
        var package = Sids.EnsureProfile();
        Assert.StartsWith("S-1-15-2-", package);
        Assert.Equal(package, Sids.EnsureProfile());
        Assert.Equal(package, Sids.Package());
        var a = Sids.RootCapability(@"C:\work\a");
        Assert.StartsWith("S-1-15-3-1024-", a);
        Assert.Equal(a, Sids.RootCapability("c:/work/a/"));
        Assert.NotEqual(a, Sids.RootCapability(@"C:\work\b"));
        Assert.Contains(@"\Packages\", Sids.FolderPath(package), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void A_job_carries_only_its_own_root_capabilities_and_internet_only_when_granted()
    {
        var offline = Policy.Parse("""
            {"schema":"vool.windows_confinement.v1","write_roots":["C:\\work\\a","C:\\work\\a\\"],
             "temp_dir":"C:\\work\\a\\t","status_path":"C:\\s\\status.json"}
            """);
        Assert.Equal(new[] { Sids.RootCapability(@"C:\work\a") }, Sids.CapabilitiesFor(offline));
        var online = offline with { Network = true };
        Assert.Equal(new[] { Sids.RootCapability(@"C:\work\a"), Sids.InternetClientSid }, Sids.CapabilitiesFor(online));
    }
}
