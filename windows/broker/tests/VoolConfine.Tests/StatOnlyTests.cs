using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;
using VoolConfine.Core;
using Xunit;
using static VoolConfine.Tests.LauncherTestSupport;

namespace VoolConfine.Tests;

/// <summary>
/// stat_only_paths: the folder above a pytest job's folder, which pytest 9.1.1 stats on every run
/// (Session._collect_path(root) asks for root.parent's conftests, and _get_directory calls
/// parent.is_file()). The job gets one explicit allow of FILE_READ_ATTRIBUTES | SYNCHRONIZE with no
/// inheritance flags, for that folder's own stat capability, on that folder only, for the length of
/// one run. Proven by the kernel (a lowbox token) and by the shipped launcher.
/// </summary>
public class StatOnlyTests
{
    const uint FileReadAttributes = 0x80;

    static (Policy Policy, ConfinementSids Sids, SecurityIdentifier Stat) StatPolicy(AclFixture f, string statPath)
    {
        var policy = Policy.Parse(JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["schema"] = Policy.Schema,
            ["write_roots"] = new[] { f.Root },
            ["stat_only_paths"] = new[] { statPath },
            ["temp_dir"] = Path.Combine(f.Root, ".vool-tmp"),
            ["status_path"] = Path.Combine(f.Outside, "status", "status.json"),
        }));
        var stat = AclTest.CapabilitySid(Identity.StatCapabilityName(statPath));
        var sids = new ConfinementSids(f.Package,
            new Dictionary<string, SecurityIdentifier>(StringComparer.Ordinal) { [f.Root] = f.Capability }, null,
            new Dictionary<string, SecurityIdentifier>(StringComparer.Ordinal) { [statPath] = stat });
        return (policy, sids, stat);
    }

    static List<CommonAce> AcesFor(string path, SecurityIdentifier sid)
    {
        using var handle = AclNative.OpenRoot(path, writeDac: false);
        var acl = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0).DiscretionaryAcl!;
        var found = new List<CommonAce>();
        foreach (GenericAce ace in acl)
            if (ace is CommonAce common && common.SecurityIdentifier == sid) found.Add(common);
        return found;
    }

    static bool CanStat(JobToken job, string path) => job.Open(path, FileReadAttributes) == 0;

    // ---- the policy

    [Fact]
    public void A_policy_names_stat_only_paths_outside_every_root_and_none_by_default()
    {
        var none = Policy.Parse("""
            {"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\mark\\proj"],
             "temp_dir":"C:\\u\\mark\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}
            """);
        Assert.Empty(none.StatOnlyPaths);
        var one = Policy.Parse("""
            {"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\mark\\proj"],"stat_only_paths":["C:\\u\\mark"],
             "temp_dir":"C:\\u\\mark\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}
            """);
        Assert.Equal(@"C:\u\mark", Assert.Single(one.StatOnlyPaths));
    }

    [Theory]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\proj"],"stat_only_paths":["C:\\u\\proj"],"temp_dir":"C:\\u\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\proj"],"stat_only_paths":["C:\\u\\proj\\sub"],"temp_dir":"C:\\u\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\proj"],"stat_only_paths":["C:\\"],"temp_dir":"C:\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\proj"],"deny_all_paths":["C:\\vool"],"stat_only_paths":["C:\\vool\\state"],"temp_dir":"C:\\u\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\proj"],"stat_only_paths":["C:\\u\\..\\x"],"temp_dir":"C:\\u\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}""")]
    public void A_stat_only_path_inside_a_root_a_denied_path_or_at_a_volume_root_is_refused(string json)
    {
        Assert.Throws<PolicyException>(() => Policy.Parse(json));
    }

    [Fact]
    public void Only_a_job_naming_a_stat_only_path_carries_its_capability()
    {
        var plain = Policy.Parse("""
            {"schema":"vool.windows_confinement.v1","write_roots":["C:\\u\\mark\\proj"],
             "temp_dir":"C:\\u\\mark\\proj\\.t","status_path":"C:\\tmp\\vc\\status.json"}
            """);
        var withStat = plain with { StatOnlyPaths = [@"C:\u\mark"] };
        var stat = Sids.CapabilitySid(Identity.StatCapabilityName(@"C:\u\mark"));
        Assert.DoesNotContain(stat, Sids.CapabilitiesFor(plain));
        Assert.Contains(stat, Sids.CapabilitiesFor(withStat));
        Assert.NotEqual(Identity.ReadCapabilityName(@"C:\u\mark"), Identity.StatCapabilityName(@"C:\u\mark"));
    }

    // ---- the ACE

    [Fact]
    public void The_grant_is_one_read_attributes_ace_with_no_inheritance_on_that_folder_only()
    {
        using var f = new AclFixture();
        File.WriteAllText(f.Out("beside.txt"), "x");
        var (policy, sids, stat) = StatPolicy(f, f.Base);
        Acl.GrantStatOnly(policy, sids);
        var ace = Assert.Single(AcesFor(f.Base, stat));
        Assert.Equal(AceQualifier.AccessAllowed, ace.AceQualifier);
        Assert.Equal(0x00100080, ace.AccessMask);
        Assert.Equal(AclRules.StatOnlyMask, ace.AccessMask);
        Assert.Equal(AceFlags.None, ace.AceFlags);
        // Nothing inside the folder got anything for that capability.
        Assert.Empty(AcesFor(f.Root, stat));
        Assert.Empty(AcesFor(f.Outside, stat));
        Acl.VerifyStatOnly(policy, sids);
        // Idempotent: a second grant changes nothing.
        Assert.Equal(0, Acl.GrantStatOnly(policy, sids).Updated);
        Assert.Single(AcesFor(f.Base, stat));
        Acl.RevokeStatOnly(policy, sids);
    }

    [Fact]
    public void The_job_may_stat_the_folder_but_never_list_it_nor_open_anything_in_it()
    {
        using var f = new AclFixture();
        File.WriteAllText(f.Out("beside.txt"), "x");
        var (policy, sids, stat) = StatPolicy(f, f.Base);
        using (var before = JobToken.Create(f.Package, f.Capability, stat))
            Assert.False(CanStat(before, f.Base), "the harness must show the stat denied before the grant");
        Acl.GrantStatOnly(policy, sids);
        try
        {
            using var job = JobToken.Create(f.Package, f.Capability, stat);
            Assert.True(CanStat(job, f.Base));
            Assert.False(job.CanRead(f.Base), "FILE_LIST_DIRECTORY on the folder");
            Assert.False(job.CanReadControl(f.Base));
            Assert.False(job.CanWriteDac(f.Base));
            Assert.False(job.CanCreateFile(Path.Combine(f.Base, "planted.txt")));
            Assert.False(CanStat(job, f.Outside), "the ACE is not inherited by what is inside");
            Assert.False(job.CanRead(f.Out("beside.txt")));
            // Another job, without the stat capability, gets nothing from the ACE.
            using var other = JobToken.Create(f.Package, f.Capability);
            Assert.False(CanStat(other, f.Base));
        }
        finally { Acl.RevokeStatOnly(policy, sids); }
    }

    [Fact]
    public void Revoke_takes_the_ace_off_and_leaves_the_dacl_as_it_was()
    {
        using var f = new AclFixture();
        var (policy, sids, stat) = StatPolicy(f, f.Base);
        var before = AclTest.Sddl(f.Base);
        Acl.GrantStatOnly(policy, sids);
        Assert.NotEqual(before, AclTest.Sddl(f.Base));
        Acl.RevokeStatOnly(policy, sids);
        Assert.Empty(AcesFor(f.Base, stat));
        Assert.Equal(before, AclTest.Sddl(f.Base));
        using var job = JobToken.Create(f.Package, f.Capability, stat);
        Assert.False(CanStat(job, f.Base));
        var missing = Assert.Throws<AclException>(() => Acl.VerifyStatOnly(policy, sids));
        Assert.Equal("grant_missing", missing.Code);
    }

    [Fact]
    public void A_widened_or_inheritable_stat_ace_fails_verify_and_grant_puts_back_the_exact_one()
    {
        using var f = new AclFixture();
        var (policy, sids, stat) = StatPolicy(f, f.Base);
        AclTest.AddAce(f.Base, AceQualifier.AccessAllowed, AclRules.StatOnlyMask, stat, AceFlags.ObjectInherit | AceFlags.ContainerInherit);
        Assert.Equal("grant_missing", Assert.Throws<AclException>(() => Acl.VerifyStatOnly(policy, sids)).Code);
        Acl.GrantStatOnly(policy, sids);
        Assert.Equal(AceFlags.None, Assert.Single(AcesFor(f.Base, stat)).AceFlags);
        AclTest.AddAce(f.Base, AceQualifier.AccessAllowed, AclRules.ReadExecuteMask, stat, AceFlags.None);
        Assert.Equal("grant_missing", Assert.Throws<AclException>(() => Acl.VerifyStatOnly(policy, sids)).Code);
        Acl.GrantStatOnly(policy, sids);
        Assert.Equal(AclRules.StatOnlyMask, Assert.Single(AcesFor(f.Base, stat)).AccessMask);
        Acl.RevokeStatOnly(policy, sids);
    }

    [Fact]
    public void The_revoke_form_takes_stat_only_aces_off_too()
    {
        using var f = new AclFixture();
        var (policy, sids, stat) = StatPolicy(f, f.Base);
        Acl.GrantStatOnly(policy, sids);
        Acl.Revoke(policy, sids, includeShared: false);
        Assert.Empty(AcesFor(f.Base, stat));
    }

    // ---- the shipped launcher: granted for the run, gone after it

    [Fact]
    public void The_shipped_launcher_grants_the_stat_for_the_run_and_takes_it_off_after()
    {
        using var box = new ConfineSandbox(grantWork: false);
        var stat = new SecurityIdentifier(Sids.StatCapability(box.Top));
        var policyPath = WritePolicy(box, box.Policy(null));
        var (granted, grantOutput) = RunPlain(RealLauncherExe(), ["grant", "--policy", policyPath]);
        Assert.True(granted == 0, $"grant exit {granted}: {grantOutput}");
        try
        {
            // GetFileAttributesEx on the folder: what CPython's os.stat asks (FILE_READ_ATTRIBUTES on it).
            var probe = PowerShellArgs($"if ([System.IO.Directory]::Exists('{box.Top}')) {{ exit 0 }} else {{ exit 9 }}");
            // Control: without stat_only_paths the box cannot stat the folder above its root.
            var denied = Launch(box, probe, host: Host.Real);
            AssertStartedAndFinished(denied, 9);
            var allowed = Launch(box, probe, change: p => p["stat_only_paths"] = new[] { box.Top }, host: Host.Real);
            AssertStartedAndFinished(allowed, 0);
            Assert.Empty(AcesFor(box.Top, stat));
            Assert.Empty(AcesFor(box.Outside, stat));
        }
        finally
        {
            var (revoked, revokeOutput) = RunPlain(RealLauncherExe(), ["revoke", "--policy", policyPath]);
            Assert.True(revoked == 0, $"revoke exit {revoked}: {revokeOutput}");
        }
    }
}
