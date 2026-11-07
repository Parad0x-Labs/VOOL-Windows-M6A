using System.Text.Json;
using VoolConfine.Core;
using Xunit;
using static VoolConfine.Tests.LauncherTestSupport;

namespace VoolConfine.Tests;

/// <summary>
/// The scan form (docs/WINDOWS_CONFINEMENT.md section 2): `scan --policy p` runs the repository scan
/// alone, unconfined and with no child, and rewrites only new_repositories and repositories_scanned.
/// </summary>
public class LauncherScanTests
{
    static (int Code, string Output) Scan(ConfineSandbox box, Host host = Host.Pass)
    {
        var policyPath = WritePolicy(box, box.Policy(null));
        string[] args = host == Host.Real ? ["scan", "--policy", policyPath] : [host == Host.Pass ? "pass" : "fail-verify", "scan", "--policy", policyPath];
        return RunPlain(host == Host.Real ? RealLauncherExe() : TestHostExe(), args);
    }

    static JsonElement Status(ConfineSandbox box) => JsonDocument.Parse(File.ReadAllText(box.StatusPath)).RootElement.Clone();

    [Fact]
    public void A_clean_scan_is_0_and_keeps_the_run_record()
    {
        using var box = new ConfineSandbox();
        AssertStartedAndFinished(Launch(box, [Cmd, "/c", "exit 7"]), 7);
        var (code, output) = Scan(box);
        Assert.True(code == 0, $"scan exit {code}: {output}");
        var status = Status(box);
        Assert.Equal(ExitCodes.StatusSchema, status.GetProperty("schema").GetString());
        Assert.True(status.GetProperty("child_started").GetBoolean());
        Assert.Equal(7, status.GetProperty("exit_code").GetInt32());
        Assert.Empty(status.GetProperty("new_repositories").EnumerateArray());
        Assert.True(status.GetProperty("repositories_scanned").GetBoolean());
    }

    [Fact]
    public void An_unprotected_repository_is_123_and_listed_after_a_run_that_never_scanned()
    {
        using var box = new ConfineSandbox();
        // A refused run leaves a record with repositories_scanned false, as a killed launcher would.
        AssertRefused(Launch(box, [Cmd, "/c", "exit 0"], host: Host.FailVerify), ExitCodes.SetupFailed);
        Assert.False(Status(box).GetProperty("repositories_scanned").GetBoolean());
        var git = Path.Combine(box.Work, ".git");
        Directory.CreateDirectory(git);
        var (code, output) = Scan(box);
        Assert.True(code == ExitCodes.NewRepository, $"scan exit {code}: {output}");
        Assert.Contains(ExitCodes.RefusedMarker, output);
        var status = Status(box);
        Assert.False(status.GetProperty("child_started").GetBoolean(), "the scan must not change child_started");
        Assert.Equal(git, Assert.Single(status.GetProperty("new_repositories").EnumerateArray()).GetString(), ignoreCase: true);
        Assert.True(status.GetProperty("repositories_scanned").GetBoolean());
    }

    [Fact]
    public void A_scan_with_no_status_record_is_125_and_writes_none()
    {
        using var box = new ConfineSandbox();
        var (code, output) = Scan(box);
        Assert.True(code == ExitCodes.SetupFailed, $"scan exit {code}: {output}");
        Assert.Contains(ExitCodes.RefusedMarker, output);
        Assert.False(File.Exists(box.StatusPath), "the scan invented a run record");
    }

    [Fact]
    public void A_scan_over_a_foreign_record_is_125_and_leaves_it()
    {
        using var box = new ConfineSandbox();
        const string Foreign = """{"schema":"something.else","child_started":true}""";
        File.WriteAllText(box.StatusPath, Foreign);
        var (code, output) = Scan(box);
        Assert.True(code == ExitCodes.SetupFailed, $"scan exit {code}: {output}");
        Assert.Equal(Foreign, File.ReadAllText(box.StatusPath));
    }

    [Fact]
    public void The_real_exe_scan_finds_a_repository_without_the_deny()
    {
        // The shipped scan (AclChecks): a .git that never got the grant form's deny is unprotected.
        using var box = new ConfineSandbox();
        AssertStartedAndFinished(Launch(box, [Cmd, "/c", "exit 0"]), 0);
        Directory.CreateDirectory(Path.Combine(box.Work, "planted", ".git"));
        var (code, output) = Scan(box, Host.Real);
        Assert.True(code == ExitCodes.NewRepository, $"scan exit {code}: {output}");
        var status = Status(box);
        Assert.True(status.GetProperty("repositories_scanned").GetBoolean());
        Assert.Contains(status.GetProperty("new_repositories").EnumerateArray(),
            entry => (entry.GetString() ?? "").Contains("planted", StringComparison.OrdinalIgnoreCase));
    }
}
