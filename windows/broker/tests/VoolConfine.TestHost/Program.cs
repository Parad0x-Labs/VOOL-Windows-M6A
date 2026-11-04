using VoolConfine.Core;

// Test-only host for VoolConfine.Tests: the same LauncherCli and Launcher as VoolConfine.exe, with the
// chunk 2 checks swapped for ones the test picks with the first argument. It is not built by
// publish.ps1 and is never shipped. VoolConfine.exe itself keeps PendingChecks until chunk 2 lands.
//   pass         ACLs count as verified; the scan reports a .git that appears directly in a write root,
//                and the scan form reports every .git directly in a write root as unprotected
//   fail-verify  the ACL verify misses, so the run must be 125 with no child
if (args.Length == 0) return LauncherCli.Refuse(Console.Error, ExitCodes.PolicyInvalid, "usage: VoolConfine.TestHost.exe pass|fail-verify <launcher args>");
IConfinementChecks checks = args[0] switch
{
    "pass" => new TestChecks(failVerify: false),
    "fail-verify" => new TestChecks(failVerify: true),
    _ => PendingChecks.Instance,
};
return LauncherCli.Run(args[1..], checks,
    grant: _ => LauncherCli.Refuse(Console.Error, ExitCodes.SetupFailed, "grant is not in the test host"),
    revoke: _ => LauncherCli.Refuse(Console.Error, ExitCodes.SetupFailed, "revoke is not in the test host"),
    Console.Error);

sealed class TestChecks(bool failVerify) : IConfinementChecks
{
    public void VerifyAcls(Policy policy, string packageSid)
    {
        if (failVerify) throw new SetupException("test host: the ACL verify missed");
    }

    public IRepositoryScan StartRepositoryScan(Policy policy) => new TopLevelGitScan(policy.WriteRoots);

    public IReadOnlyList<string> UnprotectedRepositories(Policy policy) =>
        policy.WriteRoots.Select(r => Path.Combine(r, ".git")).Where(Path.Exists).ToList();
}

sealed class TopLevelGitScan : IRepositoryScan
{
    readonly List<string> before;
    readonly IReadOnlyList<string> roots;

    public TopLevelGitScan(IReadOnlyList<string> roots)
    {
        this.roots = roots;
        before = Present();
    }

    List<string> Present() => roots.Select(r => Path.Combine(r, ".git")).Where(Path.Exists).ToList();

    public IReadOnlyList<string> NewRepositories() => Present().Except(before, StringComparer.OrdinalIgnoreCase).ToList();
}
