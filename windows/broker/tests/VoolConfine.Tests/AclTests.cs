using System.Security.AccessControl;
using VoolConfine.Core;
using Xunit;

namespace VoolConfine.Tests;

/// <summary>
/// Chunk 2 (ACL), proven by the kernel: each "job" call below opens a real file while the thread
/// impersonates a lowbox token (see JobToken). Windows only, like the rest of this project.
/// </summary>
public class AclTests
{
    private static void Touch(string path, string text = "x")
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text);
    }

    private static void MakeRepository(string gitDir)
    {
        Touch(Path.Combine(gitDir, "config"), "[core]\n");
        Touch(Path.Combine(gitDir, "HEAD"), "ref: refs/heads/main\n");
        Directory.CreateDirectory(Path.Combine(gitDir, "hooks"));
        Directory.CreateDirectory(Path.Combine(gitDir, "objects", "ab"));
        Touch(Path.Combine(gitDir, "objects", "ab", "cdef"));
        Directory.CreateDirectory(Path.Combine(gitDir, "refs", "heads"));
    }

    // ---- the harness itself: without these two, every "cannot" and "can" below would prove nothing

    [Fact]
    public void Before_grant_the_job_can_neither_read_nor_write_the_workspace()
    {
        using var f = new AclFixture();
        Touch(f.In("a.txt"));
        using var job = f.Job();
        Assert.False(job.CanRead(f.In("a.txt")));
        Assert.False(job.CanWrite(f.In("a.txt")));
        Assert.False(job.CanCreateFile(f.In("new.txt")));
    }

    [Fact]
    public void After_grant_the_job_reads_and_writes_old_and_new_files_in_its_root()
    {
        using var f = new AclFixture();
        Touch(f.In("a.txt"));
        Touch(f.In("sub", "deeper", "b.txt"));
        f.Grant();
        using var job = f.Job();
        Assert.True(job.CanRead(f.In("a.txt")));
        Assert.True(job.CanWrite(f.In("a.txt")));
        Assert.True(job.CanWrite(f.In("sub", "deeper", "b.txt")));
        Assert.True(job.CanDelete(f.In("sub", "deeper", "b.txt")));
        Assert.True(job.CanCreateFile(f.In("new.txt")));
        Assert.True(job.CanCreateDirectory(f.In("made-by-job")));
        Assert.True(job.CanCreateFile(f.In("made-by-job", "inside.txt")));
        Assert.True(job.CanWrite(f.In("made-by-job", "inside.txt")));
        // A file the person makes after the grant inherits it too.
        Touch(f.In("sub", "later.txt"));
        Assert.True(job.CanWrite(f.In("sub", "later.txt")));
    }

    [Fact]
    public void The_job_cannot_write_or_read_outside_its_roots()
    {
        using var f = new AclFixture();
        Touch(f.Out("secret.txt"));
        f.Grant();
        using var job = f.Job();
        Assert.False(job.CanRead(f.Out("secret.txt")));
        Assert.False(job.CanWrite(f.Out("secret.txt")));
        Assert.False(job.CanCreateFile(f.Out("planted.txt")));
        Assert.False(job.CanCreateFile(Path.Combine(f.Base, "beside-the-root.txt")));
    }

    [Fact]
    public void The_job_cannot_change_any_acl_nor_take_owner_rights()
    {
        using var f = new AclFixture();
        Touch(f.In("a.txt"));
        MakeRepository(f.In(".git"));
        Touch(f.Out("secret.txt"));
        f.Grant();
        using var job = f.Job();
        Assert.False(job.CanWriteDac(f.In("a.txt")));
        Assert.False(job.CanWriteDac(f.In(".git")));
        Assert.False(job.CanWriteDac(f.In(".git", "config")));
        Assert.False(job.CanWriteDac(f.Root));
        // The person owns the outside file; owner rights must not reach a lowbox token.
        Assert.False(job.CanWriteDac(f.Out("secret.txt")));
        Assert.False(job.CanReadControl(f.Out("secret.txt")));
    }

    // ---- .git

    [Fact]
    public void Git_metadata_is_readable_but_never_writable()
    {
        using var f = new AclFixture();
        MakeRepository(f.In(".git"));
        Touch(f.In("tracked.txt"));
        var report = f.Grant();
        Assert.Contains(report.Repositories, r => r.Kind == RepoKind.GitDirectory && r.Path == f.In(".git"));
        using var job = f.Job();
        Assert.True(job.CanWrite(f.In("tracked.txt")));
        Assert.True(job.CanRead(f.In(".git", "config")));
        Assert.False(job.CanWrite(f.In(".git", "config")));
        Assert.False(job.CanAppend(f.In(".git", "config")));
        Assert.False(job.CanWrite(f.In(".git", "objects", "ab", "cdef")));
        Assert.False(job.CanDelete(f.In(".git", "config")));
        Assert.False(job.CanCreateFile(f.In(".git", "hooks", "pre-commit")));
        Assert.False(job.CanCreateFile(f.In(".git", "new-file")));
        Assert.False(job.CanCreateDirectory(f.In(".git", "new-dir")));
        Assert.False(job.CanDelete(f.In(".git")));
        Assert.False(job.CanRename(f.In(".git"), f.In("git-moved")));
        Assert.False(job.CanRename(f.In(".git", "config"), f.In("config-moved")));
        // What the person's git makes in .git after the grant is denied too (inherited).
        Touch(f.In(".git", "refs", "heads", "main"));
        Assert.False(job.CanWrite(f.In(".git", "refs", "heads", "main")));
        Directory.CreateDirectory(f.In(".git", "objects", "ef"));
        Touch(f.In(".git", "objects", "ef", "0123"));
        Assert.False(job.CanWrite(f.In(".git", "objects", "ef", "0123")));
    }

    [Fact]
    public void Nested_and_variant_repositories_are_all_denied()
    {
        using var f = new AclFixture();
        MakeRepository(f.In("vendor", "lib", ".git"));
        MakeRepository(f.In("upper", ".GIT"));
        Touch(f.In("worktree", ".git"), "gitdir: C:/elsewhere/.git/worktrees/w\n");
        MakeRepository(f.In("bare.git-less"));
        // ".git." is a different NTFS name that Win32 callers open as ".git".
        Directory.CreateDirectory(f.In("dotted"));
        Assert.True(AclTest.CreateDirectoryW(@"\\?\" + f.In("dotted", ".git."), 0));
        var report = f.Grant();
        Assert.Equal(5, report.Repositories.Count);
        Assert.All(report.Repositories, r => Assert.True(r.Protected, r.Path));
        Assert.Contains(report.Repositories, r => r.Kind == RepoKind.BareRepository && r.Path == f.In("bare.git-less"));
        Assert.Contains(report.Repositories, r => r.Kind == RepoKind.GitFile && r.Path == f.In("worktree", ".git"));
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("vendor", "lib", ".git", "config")));
        Assert.False(job.CanCreateFile(f.In("upper", ".GIT", "hooks", "post-checkout")));
        Assert.False(job.CanWrite(f.In("worktree", ".git")));
        Assert.False(job.CanDelete(f.In("worktree", ".git")));
        Assert.False(job.CanWrite(f.In("bare.git-less", "config")));
        Assert.False(job.CanCreateFile(@"\\?\" + f.In("dotted", ".git.", "config")));
        Assert.True(job.CanCreateFile(f.In("vendor", "lib", "ok.txt")));
    }

    [Fact]
    public void A_repository_the_job_makes_is_found_after_it_and_refused()
    {
        using var f = new AclFixture();
        MakeRepository(f.In(".git"));
        f.Grant();
        Assert.Empty(Acl.UnprotectedRepositories(f.MakePolicy(), f.Sids()));
        using (var job = f.Job())
        {
            // Nested git init with a planted hook, a .git pointer file, and a bare layout.
            Assert.True(job.CanCreateDirectory(f.In("sub")));
            Assert.True(job.CanCreateDirectory(f.In("sub", ".git")));
            Assert.True(job.CanCreateDirectory(f.In("sub", ".git", "hooks")));
            Assert.True(job.CanCreateFile(f.In("sub", ".git", "hooks", "post-checkout")));
            Assert.True(job.CanCreateDirectory(f.In("ptr")));
            Assert.True(job.CanCreateFile(f.In("ptr", ".Git")));
            Assert.True(job.CanCreateDirectory(f.In("bare")));
            Assert.True(job.CanCreateFile(f.In("bare", "HEAD")));
            Assert.True(job.CanCreateDirectory(f.In("bare", "objects")));
            Assert.True(job.CanCreateDirectory(f.In("bare", "refs")));
        }
        var found = Acl.UnprotectedRepositories(f.MakePolicy(), f.Sids());
        Assert.Equal(3, found.Count);
        Assert.Contains(found, r => r.Path == f.In("sub", ".git") && r.Kind == RepoKind.GitDirectory);
        Assert.Contains(found, r => r.Path == f.In("ptr", ".Git") && r.Kind == RepoKind.GitFile);
        Assert.Contains(found, r => r.Path == f.In("bare") && r.Kind == RepoKind.BareRepository);
        var refused = Assert.Throws<AclException>(() => Acl.Verify(f.MakePolicy(), f.Sids()));
        Assert.Equal("repo_unprotected", refused.Code);
    }

    [Fact]
    public void A_link_named_git_is_refused()
    {
        using var f = new AclFixture();
        MakeRepository(f.Out("real-repo"));
        AclTest.Junction(f.In(".git"), f.Out("real-repo"));
        Assert.Equal("repo_is_link", Assert.Throws<AclException>(() => f.Grant()).Code);
        var found = Assert.Single(Acl.UnprotectedRepositories(f.MakePolicy(), f.Sids()));
        Assert.Equal(RepoKind.GitLink, found.Kind);
    }

    [Fact]
    public void A_git_folder_with_a_null_dacl_is_refused()
    {
        using var f = new AclFixture();
        MakeRepository(f.In(".git"));
        AclTest.SetSddl(f.In(".git"), "D:NO_ACCESS_CONTROL");
        Assert.Equal("null_dacl", Assert.Throws<AclException>(() => f.Grant()).Code);
    }

    // ---- deny paths

    [Fact]
    public void Deny_paths_beat_the_inherited_grant()
    {
        using var f = new AclFixture();
        Touch(f.In("vendor-code", "lib.py"));
        Touch(f.In("state", "key.json"));
        Touch(f.Out("runtime", "token"));
        f.AddDenyWrite(f.In("vendor-code"));
        f.AddDenyAll(f.In("state"));
        f.AddDenyAll(f.Out("runtime"));
        f.Grant();
        using var job = f.Job();
        Assert.True(job.CanRead(f.In("vendor-code", "lib.py")));
        Assert.False(job.CanWrite(f.In("vendor-code", "lib.py")));
        Assert.False(job.CanCreateFile(f.In("vendor-code", "new.py")));
        Assert.False(job.CanRead(f.In("state", "key.json")));
        Assert.False(job.CanWrite(f.In("state", "key.json")));
        Assert.False(job.CanCreateFile(f.In("state", "new.json")));
        Assert.False(job.CanRead(f.Out("runtime", "token")));
        // Made by VOOL after the grant: denied by inheritance.
        Touch(f.In("state", "later.json"));
        Assert.False(job.CanRead(f.In("state", "later.json")));
    }

    [Fact]
    public void A_deny_path_that_is_missing_under_a_root_is_refused()
    {
        using var f = new AclFixture();
        f.AddDenyAll(f.In("state-not-made-yet"));
        Assert.Equal("deny_path_missing", Assert.Throws<AclException>(() => f.Grant()).Code);
    }

    [Fact]
    public void A_root_inside_a_denied_path_is_refused()
    {
        using var f = new AclFixture();
        f.AddDenyAll(f.Base);
        Assert.Equal("root_inside_denied", Assert.Throws<AclException>(() => f.Grant()).Code);
    }

    // ---- links

    [Fact]
    public void A_junction_to_an_outside_folder_gives_no_write_there()
    {
        using var f = new AclFixture();
        Touch(f.Out("target", "existing.txt"));
        AclTest.Junction(f.In("link"), f.Out("target"));
        var before = AclTest.SnapshotTree(f.Out("target"));
        var report = f.Grant();
        Assert.True(report.SkippedReparsePoints >= 1);
        Assert.Equal(before, AclTest.SnapshotTree(f.Out("target")));
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("link", "existing.txt")));
        Assert.False(job.CanCreateFile(f.In("link", "planted.txt")));
        // A junction the person makes after the grant: still the target's own ACL.
        AclTest.Junction(f.In("link2"), f.Out("target"));
        Assert.False(job.CanCreateFile(f.In("link2", "planted.txt")));
    }

    [Fact]
    public void A_hard_link_to_an_outside_file_is_not_widened()
    {
        using var f = new AclFixture();
        Touch(f.Out("secret.txt"), "secret");
        AclTest.HardLink(f.In("innocent.txt"), f.Out("secret.txt"));
        var before = AclTest.Sddl(f.Out("secret.txt"));
        var report = f.Grant();
        Assert.Equal(1, report.SkippedHardLinks);
        Assert.Equal(before, AclTest.Sddl(f.Out("secret.txt")));
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("innocent.txt")));
        Assert.False(job.CanRead(f.In("innocent.txt")));
        Assert.False(job.CanWrite(f.Out("secret.txt")));
    }

    [Fact]
    public void A_root_reached_through_a_junction_is_refused()
    {
        using var f = new AclFixture();
        AclTest.Junction(f.Out("alias"), f.Root);
        var policy = Policy.Parse(System.Text.Json.JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["schema"] = Policy.Schema, ["write_roots"] = new[] { f.Out("alias") },
            ["temp_dir"] = f.Out("alias", ".vool-tmp"), ["status_path"] = Path.Combine(f.Base, "st", "status.json"),
        }));
        var sids = new ConfinementSids(f.Package, new Dictionary<string, System.Security.Principal.SecurityIdentifier> { [f.Out("alias")] = f.Capability });
        Assert.Equal("reparse_point", Assert.Throws<AclException>(() => Acl.Grant(policy, sids)).Code);
    }

    // ---- identity: one capability per root

    [Fact]
    public void A_job_for_one_root_cannot_write_another_root()
    {
        using var f = new AclFixture();
        var other = Path.Combine(f.Base, "other-workspace");
        Directory.CreateDirectory(other);
        Touch(Path.Combine(other, "b.txt"));
        Touch(f.In("a.txt"));
        f.AddWriteRoot(other);
        f.Grant();
        using var jobA = f.Job();
        Assert.True(jobA.CanWrite(f.In("a.txt")));
        Assert.False(jobA.CanWrite(Path.Combine(other, "b.txt")));
        Assert.False(jobA.CanRead(Path.Combine(other, "b.txt")));
        using var jobB = JobToken.Create(f.Package, f.Sids().CapabilityFor(other));
        Assert.True(jobB.CanWrite(Path.Combine(other, "b.txt")));
        Assert.False(jobB.CanWrite(f.In("a.txt")));
    }

    [Fact]
    public void A_read_root_is_readable_and_never_writable()
    {
        using var f = new AclFixture();
        var tools = Path.Combine(f.Base, "python");
        Touch(Path.Combine(tools, "Lib", "os.py"));
        Touch(Path.Combine(tools, "python.exe"));
        f.AddReadRoot(tools);
        f.Grant();
        using var job = f.Job();
        Assert.True(job.CanRead(Path.Combine(tools, "Lib", "os.py")));
        Assert.Equal(0, job.Open(Path.Combine(tools, "python.exe"), 0x20 /* FILE_EXECUTE */));
        Assert.False(job.CanWrite(Path.Combine(tools, "Lib", "os.py")));
        Assert.False(job.CanCreateFile(Path.Combine(tools, "Lib", "planted.py")));
        Assert.False(job.CanDelete(Path.Combine(tools, "python.exe")));
    }

    [Fact]
    public void A_folder_with_a_protected_dacl_gets_no_grant()
    {
        using var f = new AclFixture();
        Touch(f.In("private", "mine.txt"));
        var me = System.Security.Principal.WindowsIdentity.GetCurrent().User!.Value;
        AclTest.SetSddl(f.In("private"), $"D:P(A;OICI;FA;;;{me})");
        AclTest.SetSddl(f.In("private", "mine.txt"), $"D:(A;;FA;;;{me})");
        var report = f.Grant();
        Assert.True(report.SkippedProtectedDacl >= 1);
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("private", "mine.txt")));
        Assert.False(job.CanCreateFile(f.In("private", "new.txt")));
    }

    // ---- verify, idempotence, revoke

    [Fact]
    public void Verify_refuses_a_root_without_its_grant()
    {
        using var f = new AclFixture();
        Assert.Equal("grant_missing", Assert.Throws<AclException>(() => Acl.Verify(f.MakePolicy(), f.Sids())).Code);
        f.Grant();
        Acl.Verify(f.MakePolicy(), f.Sids());
        Acl.Revoke(f.MakePolicy(), f.Sids(), includeShared: false);
        Assert.Equal("grant_missing", Assert.Throws<AclException>(() => Acl.Verify(f.MakePolicy(), f.Sids())).Code);
    }

    [Fact]
    public void Verify_refuses_a_root_where_every_job_could_write()
    {
        using var f = new AclFixture();
        f.Grant();
        AclTest.AddAce(f.Root, AceQualifier.AccessAllowed, 0x001F01FF, f.Package, AceFlags.ObjectInherit | AceFlags.ContainerInherit);
        Assert.Equal("package_can_write", Assert.Throws<AclException>(() => Acl.Verify(f.MakePolicy(), f.Sids())).Code);
    }

    [Fact]
    public void Verify_refuses_a_widened_grant()
    {
        using var f = new AclFixture();
        f.Grant();
        AclTest.AddAce(f.Root, AceQualifier.AccessAllowed, 0x00040000 /* WRITE_DAC */, f.Capability, AceFlags.ObjectInherit | AceFlags.ContainerInherit);
        Assert.Equal("grant_widened", Assert.Throws<AclException>(() => Acl.Verify(f.MakePolicy(), f.Sids())).Code);
    }

    [Fact]
    public void Grant_is_idempotent()
    {
        using var f = new AclFixture();
        MakeRepository(f.In(".git"));
        Touch(f.In("src", "a.py"));
        f.Grant();
        var after = AclTest.SnapshotTree(f.Root);
        var again = f.Grant();
        Assert.Equal(0, again.Updated);
        Assert.Equal(after, AclTest.SnapshotTree(f.Root));
    }

    [Fact]
    public void Revoke_puts_every_dacl_back_byte_for_byte()
    {
        using var f = new AclFixture();
        MakeRepository(f.In(".git"));
        Touch(f.In("src", "a.py"));
        Touch(f.In("vendor-code", "v.py"));
        Touch(f.Out("runtime", "token"));
        var me = System.Security.Principal.WindowsIdentity.GetCurrent().User!.Value;
        Touch(f.In("private", "p.txt"));
        AclTest.SetSddl(f.In("private"), $"D:PAI(A;OICI;FA;;;{me})");
        f.AddDenyWrite(f.In("vendor-code"));
        f.AddDenyAll(f.Out("runtime"));
        var workspace = AclTest.SnapshotTree(f.Root);
        var runtime = AclTest.SnapshotTree(f.Out("runtime"));
        f.Grant();
        Assert.NotEqual(workspace, AclTest.SnapshotTree(f.Root));
        Acl.Revoke(f.MakePolicy(), f.Sids(), includeShared: true);
        Assert.Equal(workspace, AclTest.SnapshotTree(f.Root));
        Assert.Equal(runtime, AclTest.SnapshotTree(f.Out("runtime")));
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("src", "a.py")));
    }

    [Fact]
    public void A_file_made_after_grant_loses_the_grant_on_revoke()
    {
        using var f = new AclFixture();
        f.Grant();
        Touch(f.In("later", "x.txt"));
        Acl.Revoke(f.MakePolicy(), f.Sids(), includeShared: false);
        using var job = f.Job();
        Assert.False(job.CanWrite(f.In("later", "x.txt")));
        Assert.DoesNotContain(f.Capability.Value, AclTest.Sddl(f.In("later", "x.txt")), StringComparison.Ordinal);
    }

    // ---- pure rules (no file system)

    [Theory]
    [InlineData(".git", "", true)]
    [InlineData(".GIT", "", true)]
    [InlineData(".git.", "", true)]
    [InlineData(".git ", "", true)]
    [InlineData(".git. .", "", true)]
    [InlineData("x", "GIT~1", true)]
    [InlineData(".gitignore", "GITIGN~1", false)]
    [InlineData(".github", "GITHUB~1", false)]
    [InlineData("git", "", false)]
    [InlineData("a.git", "", false)]
    public void Git_names_are_matched_as_windows_resolves_them(string name, string shortName, bool expected) =>
        Assert.Equal(expected, RepoScan.IsGitName(name, shortName));

    [Fact]
    public void Ensure_keeps_denies_first_and_is_idempotent()
    {
        var user = System.Security.Principal.WindowsIdentity.GetCurrent().User!;
        var package = AclTest.PackageSid("VOOL.Confine.Tests.rules");
        var cap = AclTest.CapabilitySid("vool.root.test");
        var acl = new RawSecurityDescriptor($"D:(D;;WD;;;BG)(A;;FA;;;{user.Value})(A;ID;FA;;;SY)").DiscretionaryAcl!;
        Assert.True(AclRules.Ensure(acl, AclRules.ExplicitDeny(package, AclRules.DenyWriteMask, true)));
        Assert.True(AclRules.Ensure(acl, AclRules.ExplicitAllow(cap, AclRules.ReadWriteMask, true)));
        Assert.True(AclRules.Ensure(acl, AclRules.PropagatedAllow(package, AclRules.ReadExecuteMask, true)));
        Assert.False(AclRules.Ensure(acl, AclRules.ExplicitDeny(package, AclRules.DenyWriteMask, true)));
        Assert.False(AclRules.Ensure(acl, AclRules.ExplicitAllow(cap, AclRules.ReadWriteMask, true)));
        Assert.False(AclRules.Ensure(acl, AclRules.PropagatedAllow(package, AclRules.ReadExecuteMask, true)));
        var order = Enumerable.Range(0, acl.Count).Select(i => (CommonAce)acl[i]).ToArray();
        Assert.Equal(package, order[0].SecurityIdentifier);
        Assert.Equal(AceQualifier.AccessDenied, order[0].AceQualifier);
        Assert.Equal(cap, order[3].SecurityIdentifier);
        Assert.Equal(package, order[5].SecurityIdentifier);
        Assert.True(AclRules.HasExplicitDeny(acl, package, AclRules.DenyWriteMask, directory: true));
        Assert.True(AclRules.HasExplicitAllow(acl, cap, AclRules.ReadWriteMask, directory: true));
        Assert.Equal(3, AclRules.RemoveAll(acl, [package, cap]));
        Assert.Equal(3, acl.Count);
    }
}
