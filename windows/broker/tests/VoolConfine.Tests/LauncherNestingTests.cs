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
}
