using System.Security.Principal;

namespace VoolConfine.Core;

/// <summary>
/// The shipped checks (docs/WINDOWS_CONFINEMENT.md section 2): the run form verifies with chunk 2's
/// Acl code before the child starts and scans after the job ends; the grant and revoke forms write
/// and remove the ACEs. Every AclException becomes a SetupException, so a miss is 125 and no child
/// starts.
/// </summary>
public sealed class AclChecks : IConfinementChecks
{
    public static readonly AclChecks Instance = new();

    /// <summary>
    /// The launcher's own folders: its folder (VoolConfine.exe and VoolConfineStart.exe) and the
    /// profile's AC\Temp.
    /// </summary>
    public static Acl.HostPaths Host(string packageSid) =>
        new(Acl.Normalize(AppContext.BaseDirectory), Path.Combine(Sids.FolderPath(packageSid), "Temp"));

    static ConfinementSids For(Policy policy, string packageSid) =>
        ConfinementSids.For(policy, packageSid, Sids.RootCapability, Sids.ReadCapability, Sids.StatCapability);

    public void VerifyAcls(Policy policy, string packageSid) => Checked(() =>
    {
        Acl.VerifyHost(Host(packageSid), new SecurityIdentifier(packageSid));
        Acl.Verify(policy, For(policy, packageSid));
        Acl.VerifyStatOnly(policy, For(policy, packageSid));
    });

    /// <summary>The run form, before VerifyAcls: this job's stat-only ACEs (one per stat_only_paths entry).</summary>
    public void GrantStatOnly(Policy policy, string packageSid) => Checked(() => { Acl.GrantStatOnly(policy, For(policy, packageSid)); });

    /// <summary>The run form, once the job's tree is gone: the same ACEs off again.</summary>
    public void RevokeStatOnly(Policy policy, string packageSid) => Checked(() => { Acl.RevokeStatOnly(policy, For(policy, packageSid)); });

    /// <summary>
    /// Stateless: every repository layout under a write root carries the deny once Verify passed,
    /// so any unprotected one after the job is one the job made.
    /// </summary>
    public IRepositoryScan StartRepositoryScan(Policy policy) => new AfterJob(policy);

    public IReadOnlyList<string> UnprotectedRepositories(Policy policy) =>
        Checked(() => Acl.UnprotectedRepositories(policy, For(policy, Sids.Package())).Select(entry => entry.Path).ToList());

    /// <summary>The grant form: the policy's ACEs, then the host folders, each verified. 0, or 125.</summary>
    public static int Grant(Policy policy, TextWriter stderr) => Form(stderr, "grant", () =>
    {
        var package = Sids.EnsureProfile();
        var report = Acl.Grant(policy, For(policy, package));
        var host = Host(package);
        // Windows makes AC\Temp on the profile's first process; before that there is nothing to close.
        Directory.CreateDirectory(host.ProfileTemp);
        Acl.GrantHost(host, new SecurityIdentifier(package));
        stderr.WriteLine($"granted: {report.Updated} updated, {report.Unchanged} unchanged, {report.Repositories.Count} repositories denied write");
    });

    /// <summary>The revoke form: exactly the policy's ACEs, its stat-only ones included (shared ones stay for other workspaces). 0, or 125.</summary>
    public static int Revoke(Policy policy, TextWriter stderr) => Form(stderr, "revoke", () =>
    {
        var report = Acl.Revoke(policy, For(policy, Sids.Package()), includeShared: false);
        stderr.WriteLine($"revoked: {report.Updated} updated");
    });

    static int Form(TextWriter stderr, string what, Action action)
    {
        try
        {
            action();
            return 0;
        }
        catch (Exception e) when (e is AclException or SetupException or IOException or UnauthorizedAccessException)
        {
            var code = e is AclException acl ? $" ({acl.Code})" : "";
            return LauncherCli.Refuse(stderr, ExitCodes.SetupFailed, $"{what} failed{code}: {e.Message}");
        }
    }

    static void Checked(Action action) => Checked(() => { action(); return 0; });

    static T Checked<T>(Func<T> action)
    {
        try { return action(); }
        catch (AclException e) { throw new SetupException($"{e.Code}: {e.Message}"); }
    }

    sealed class AfterJob(Policy policy) : IRepositoryScan
    {
        public IReadOnlyList<string> NewRepositories() => Instance.UnprotectedRepositories(policy);
    }
}
