using Microsoft.Win32.SafeHandles;
using System.Security.AccessControl;
using System.Security.Principal;

namespace VoolConfine.Core;

/// <summary>
/// The SIDs one policy's ACEs are for (docs/WINDOWS_CONFINEMENT.md section 2, "Identity"):
/// the profile's package SID (read grants, and every deny), and one capability SID per write root
/// (that root's read-write grant, and every deny under that root). RootCapabilities is keyed by the
/// write root exactly as the policy spells it.
/// </summary>
public sealed record ConfinementSids(SecurityIdentifier Package, IReadOnlyDictionary<string, SecurityIdentifier> RootCapabilities)
{
    /// <summary>
    /// The SIDs for a policy from the launcher's own derivation (VoolConfine.Core.Sids:
    /// <c>ConfinementSids.For(policy, Sids.Package(), Sids.RootCapability)</c>), so they are derived once, in one place.
    /// </summary>
    public static ConfinementSids For(Policy policy, string packageSid, Func<string, string> rootCapabilitySid) =>
        new(new SecurityIdentifier(packageSid),
            policy.WriteRoots.Distinct(StringComparer.Ordinal).ToDictionary(root => root, root => new SecurityIdentifier(rootCapabilitySid(root)), StringComparer.Ordinal));

    public SecurityIdentifier CapabilityFor(string root) =>
        RootCapabilities.TryGetValue(root, out var sid) ? sid
        : throw new AclException("sid_missing", $"No capability SID was given for write root {root}.");
}

/// <summary>What a grant or revoke did. Skipped objects carry no grant, so a job gets no access to them.</summary>
public sealed record AclReport(
    int Updated,
    int Unchanged,
    int SkippedHardLinks,
    int SkippedNoAccess,
    int SkippedReparsePoints,
    int SkippedProtectedDacl,
    IReadOnlyList<RepoEntry> Repositories);

/// <summary>
/// Grant, verify and revoke the confinement's ACEs (docs/WINDOWS_CONFINEMENT.md section 2).
///
/// - Write root: an explicit ALLOW <see cref="AclRules.ReadWriteMask"/> (OI|CI) for that root's
///   capability SID; every object already under it gets the same ACE marked inherited, appended
///   last so every deny already there still wins.
/// - Read root: the same with <see cref="AclRules.ReadExecuteMask"/> for the package SID.
/// - Every repository layout under a write root (see <see cref="RepoScan"/>), and everything inside
///   it: an explicit DENY <see cref="AclRules.DenyWriteMask"/> for the package SID AND the root's
///   capability, first in the DACL. Both are needed: Windows grants an AppContainer what its
///   package SID or any of its capabilities is allowed, each judged on its own, so a deny for one
///   SID does not stop a grant to another.
/// - deny_write_paths / deny_all_paths: an explicit DENY of write / of everything for the package
///   SID (and, under a write root, that root's capability), on the object and on everything inside
///   it. Matched by file id.
///
/// Never widened: a file with more than one hard link (the other name may be outside every root),
/// an object whose DACL is protected (and everything under it), and a reparse point (never opened
/// through, never changed). Denies still go on shared and protected objects: they only narrow.
///
/// Every object is changed by handle with SetKernelObjectSecurity, which propagates nothing.
/// The roots' own ACEs are written LAST, so a grant that stops half way leaves a root that fails
/// <see cref="Verify"/>, and no job runs.
/// Grant and revoke run unconfined, by VOOL's own user, while no job runs on those roots.
/// </summary>
public static class Acl
{
    private enum Mode { Write, Read }

    private enum DenyKind { None, Write, All }

    private readonly record struct Context(bool AllowBlocked, bool InRepo, DenyKind Deny);

    private sealed class Counts
    {
        public int Updated, Unchanged, HardLinks, NoAccess, ReparsePoints, ProtectedDacl;
        public AclReport Report(IReadOnlyList<RepoEntry> repositories) =>
            new(Updated, Unchanged, HardLinks, NoAccess, ReparsePoints, ProtectedDacl, repositories);
    }

    // ---------------------------------------------------------------- grant

    /// <summary>Writes every grant and deny the policy needs (idempotent), then verifies. Throws on any failure.</summary>
    public static AclReport Grant(Policy policy, ConfinementSids sids)
    {
        RefuseNestedWriteRoots(policy);
        var counts = new Counts();
        var denies = OpenDenyTargets(policy);
        try
        {
            foreach (var root in policy.WriteRoots)
            {
                var capability = sids.CapabilityFor(root);
                WalkGrant(Normalize(root), Mode.Write, capability, [sids.Package, capability], [capability],
                    policy.DenyWriteGit, denies.ById, counts, denyOnly: DenyKind.None);
            }
            foreach (var root in policy.ReadRoots)
                WalkGrant(Normalize(root), Mode.Read, sids.Package, [sids.Package], ContainingCapabilities(policy, sids, root),
                    false, denies.ById, counts, denyOnly: DenyKind.None);
            // A deny path under no root: the deny on the object and, since it only narrows, on
            // everything already inside it (a child could carry its own grant to some AppContainer).
            foreach (var (path, handle, kind, directory) in denies.Outside)
            {
                if (directory)
                    WalkGrant(path, Mode.Read, allowSid: null, [sids.Package], [], false, denies.ById, counts, denyOnly: kind);
                else
                    Apply(handle, path, directory, isRoot: true, Mode.Read, allowSid: null, [sids.Package], foreignAllowed: null,
                        new Context(true, false, kind), counts, linkCount: 1);
            }
        }
        finally { denies.Dispose(); }
        var repositories = Verify(policy, sids);
        return counts.Report(repositories);
    }

    /// <summary>
    /// Walks root and puts each object's ACEs in place. allowSid gets the grant (null for a deny-only
    /// walk); every deny names each SID in denySids, because the AppContainer check grants what the
    /// package SID OR any capability is allowed, each judged on its own: a deny for the package SID
    /// does not stop a write the root's capability is allowed (measured: windows-broker run 41901543557).
    /// foreignAllowed: the AppContainer SIDs, besides ours, that may hold write here (null: no check).
    /// </summary>
    private static void WalkGrant(string root, Mode mode, SecurityIdentifier? allowSid, SecurityIdentifier[] denySids,
        SecurityIdentifier[]? foreignAllowed, bool denyGit, IReadOnlyDictionary<(ulong, string), DenyKind> denyById,
        Counts counts, DenyKind denyOnly)
    {
        using var rootHandle = AclNative.OpenRoot(root, writeDac: true);
        CheckRoot(rootHandle, root);
        var rootEntries = AclNative.List(rootHandle, root);
        if (mode == Mode.Write && RepoScan.IsBareShape(rootEntries))
            throw new AclException("root_is_repository", $"Write root {root} is itself a bare repository.");
        Context rootContext;
        if (denyOnly != DenyKind.None) rootContext = new Context(true, false, denyOnly);
        else
        {
            rootContext = new Context(false, false, DenyOf(rootHandle, root, denyById));
            if (rootContext.Deny != DenyKind.None)
                throw new AclException("root_inside_denied", $"Root {root} is a denied path.");
        }
        var foreign = denyOnly == DenyKind.None ? foreignAllowed : null;
        if (foreign is not null)
        {
            // Before anything is written: a root another AppContainer SID may already write is refused whole.
            var rootAcl = new RawSecurityDescriptor(AclNative.GetDacl(rootHandle, root), 0).DiscretionaryAcl
                ?? throw new AclException("null_dacl", $"Root {root} has a NULL DACL (everyone has full access).");
            SecurityIdentifier[] allowed = allowSid is null ? foreign : [allowSid, .. foreign];
            var writer = AclRules.ForeignWriter(rootAcl, allowed);
            if (writer is not null)
                throw new AclException("foreign_writer", $"Root {root} lets another AppContainer SID ({writer.Value}) write it.");
        }

        var stack = new Stack<Frame>();
        stack.Push(new Frame(rootHandle, root, rootEntries, 0, false, rootContext));
        try
        {
            while (stack.Count > 0)
            {
                var frame = stack.Pop();
                if (frame.Index >= frame.Entries.Count) { if (frame.Own) frame.Handle.Dispose(); continue; }
                var entry = frame.Entries[frame.Index];
                stack.Push(frame with { Index = frame.Index + 1 });
                var path = frame.Path + "\\" + entry.Name;
                var gitName = RepoScan.IsGitName(entry.Name, entry.ShortName);
                var guardsRepo = mode == Mode.Write && denyGit;
                if (entry.IsReparsePoint)
                {
                    if (guardsRepo && gitName && !frame.Context.InRepo)
                        throw new AclException("repo_is_link", $"{path} is a link named .git; no ACE on a link binds what it points to.");
                    counts.ReparsePoints++;
                    continue;
                }
                var handle = AclNative.OpenChild(frame.Handle, entry.Name, entry.IsDirectory, writeDac: true);
                if (handle is null)
                {
                    if (guardsRepo && (gitName || frame.Context.InRepo))
                        throw new AclException("access_denied", $"Cannot change the DACL of {path}, which must be denied write.");
                    counts.NoAccess++;
                    continue;
                }
                var keep = false;
                try
                {
                    // The open was by handle with FILE_OPEN_REPARSE_POINT; check what was opened, not what was listed.
                    var attributes = AclNative.GetAttributes(handle, path);
                    if ((attributes & AclNative.FileAttributeReparsePoint) != 0) { counts.ReparsePoints++; continue; }
                    var directory = (attributes & AclNative.FileAttributeDirectory) != 0;
                    List<AclNative.Entry>? entries = directory ? AclNative.List(handle, path) : null;
                    var inRepo = frame.Context.InRepo || (guardsRepo && (gitName || (entries is not null && RepoScan.IsBareShape(entries))));
                    var deny = frame.Context.Deny;
                    var own = DenyOf(handle, path, denyById);
                    if (own > deny) deny = own;
                    var context = new Context(frame.Context.AllowBlocked || deny == DenyKind.All, inRepo, deny);
                    var links = directory ? 1u : AclNative.GetLinkCount(handle, path);
                    context = Apply(handle, path, directory, isRoot: false, mode, allowSid, denySids, foreign, context, counts, links);
                    if (entries is not null)
                    {
                        stack.Push(new Frame(handle, path, entries, 0, true, context));
                        keep = true;
                    }
                }
                finally { if (!keep) handle.Dispose(); }
            }
        }
        finally
        {
            while (stack.Count > 0) { var frame = stack.Pop(); if (frame.Own) frame.Handle.Dispose(); }
        }
        // Last: the root's own explicit ACE.
        Apply(rootHandle, root, directory: true, isRoot: true, mode, allowSid, denySids, foreign, rootContext, counts, linkCount: 1);
    }

    private readonly record struct Frame(SafeFileHandle Handle, string Path, List<AclNative.Entry> Entries, int Index, bool Own, Context Context);

    /// <summary>Puts this object's ACEs in place. Returns the context its children inherit.</summary>
    private static Context Apply(SafeFileHandle handle, string path, bool directory, bool isRoot, Mode mode,
        SecurityIdentifier? allowSid, SecurityIdentifier[] denySids, SecurityIdentifier[]? foreignAllowed,
        Context context, Counts counts, uint linkCount)
    {
        var descriptor = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0);
        var isProtected = (descriptor.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0;
        // A NULL DACL grants everything to everyone, every AppContainer (so every other job) included.
        var acl = descriptor.DiscretionaryAcl
            ?? throw new AclException("null_dacl", $"{path} has a NULL DACL (everyone, every AppContainer included, has full access).");
        if (foreignAllowed is not null)
        {
            // Another AppContainer SID that may write here (another root's capability: overlapping
            // workspaces; ALL APPLICATION PACKAGES): that job, or every job, could write this root.
            SecurityIdentifier[] allowed = allowSid is null ? foreignAllowed : [allowSid, .. foreignAllowed];
            var writer = AclRules.ForeignWriter(acl, allowed);
            if (writer is not null)
                throw new AclException("foreign_writer", $"{path} lets another AppContainer SID ({writer.Value}) write it.");
        }
        if (isProtected && !isRoot && !context.AllowBlocked)
        {
            counts.ProtectedDacl++;
            context = context with { AllowBlocked = true };
        }
        var changed = false;
        foreach (var sid in denySids)
        {
            if (context.Deny == DenyKind.All) changed |= AclRules.Ensure(acl, AclRules.ExplicitDeny(sid, AclRules.DenyAllMask, directory));
            if (context.Deny == DenyKind.Write || context.InRepo) changed |= AclRules.Ensure(acl, AclRules.ExplicitDeny(sid, AclRules.DenyWriteMask, directory));
        }
        if (allowSid is not null && !context.AllowBlocked)
        {
            if (linkCount > 1) counts.HardLinks++;
            else
            {
                var mask = mode == Mode.Write ? AclRules.ReadWriteMask : AclRules.ReadExecuteMask;
                changed |= AclRules.Ensure(acl, isRoot ? AclRules.ExplicitAllow(allowSid, mask, directory) : AclRules.PropagatedAllow(allowSid, mask, directory));
            }
        }
        if (changed)
        {
            descriptor.DiscretionaryAcl = acl;
            var bytes = new byte[descriptor.BinaryLength];
            descriptor.GetBinaryForm(bytes, 0);
            AclNative.SetDacl(handle, bytes, isProtected, path);
            counts.Updated++;
        }
        else counts.Unchanged++;
        return context;
    }

    // ---------------------------------------------------------------- verify

    /// <summary>
    /// The check the launcher runs before every job (a miss is exit 125). Throws unless: each root
    /// is the folder the policy names (no link on the way, on a volume that keeps ACLs); no write
    /// root lies inside another; each write root holds its capability's exact grant and nothing
    /// wider; no other AppContainer SID may write a root (a read root may be written only by the
    /// capability of a write root it lies in); each read root holds the package's read grant; every
    /// repository layout under a write root carries the deny for the package SID and the root's
    /// capability (when deny_write_git); every deny path carries its deny.
    /// Returns the repository layouts found, all protected.
    /// </summary>
    public static IReadOnlyList<RepoEntry> Verify(Policy policy, ConfinementSids sids)
    {
        RefuseNestedWriteRoots(policy);
        var repositories = new List<RepoEntry>();
        foreach (var policyRoot in policy.WriteRoots)
        {
            var root = Normalize(policyRoot);
            var capability = sids.CapabilityFor(policyRoot);
            var acl = ReadRootAcl(root);
            if (!AclRules.HasExplicitAllow(acl, capability, AclRules.ReadWriteMask, directory: true))
                throw new AclException("grant_missing", $"Write root {root} does not hold its read-write grant (run grant).");
            if ((AclRules.AllowedBits(acl, capability) & ~AclRules.ReadWriteMask) != 0)
                throw new AclException("grant_widened", $"Write root {root} grants its capability more than read-write.");
            if ((AclRules.AllowedBits(acl, sids.Package) & AclRules.WriteOrGenericWriteBits) != 0)
                throw new AclException("package_can_write", $"Write root {root} lets every confined job write it.");
            var writer = AclRules.ForeignWriter(acl, [capability]);
            if (writer is not null)
                throw new AclException("foreign_writer", $"Write root {root} lets another AppContainer SID ({writer.Value}) write it.");
        }
        foreach (var policyRoot in policy.ReadRoots)
        {
            var root = Normalize(policyRoot);
            var acl = ReadRootAcl(root);
            if (!AclRules.HasExplicitAllow(acl, sids.Package, AclRules.ReadExecuteMask, directory: true))
                throw new AclException("grant_missing", $"Read root {root} does not hold its read grant (run grant).");
            if ((AclRules.AllowedBits(acl, sids.Package) & AclRules.WriteOrGenericWriteBits) != 0)
                throw new AclException("package_can_write", $"Read root {root} lets every confined job write it.");
            var writer = AclRules.ForeignWriter(acl, ContainingCapabilities(policy, sids, policyRoot));
            if (writer is not null)
                throw new AclException("foreign_writer", $"Read root {root} lets an AppContainer SID ({writer.Value}) write it.");
        }
        CheckDenyTargets(policy, sids);
        if (policy.DenyWriteGit)
        {
            var unprotected = new List<RepoEntry>();
            foreach (var policyRoot in policy.WriteRoots)
                foreach (var entry in RepoScan.Scan(Normalize(policyRoot), [sids.Package, sids.CapabilityFor(policyRoot)]))
                    (entry.Protected ? repositories : unprotected).Add(entry);
            if (unprotected.Count > 0)
                throw new AclException("repo_unprotected",
                    "Repository layouts under a write root are not denied write: " + string.Join("; ", unprotected.Select(e => $"{e.Path} ({e.Kind})")));
        }
        return repositories;
    }

    /// <summary>
    /// The repository layouts under the policy's write roots that do NOT carry the deny. Run after a
    /// job: any entry here was made while it ran (nested git init, a planted .git file or link, a
    /// bare repo), and the result is refused (exit 123). Throws when a folder cannot be listed.
    /// </summary>
    public static IReadOnlyList<RepoEntry> UnprotectedRepositories(Policy policy, ConfinementSids sids) =>
        policy.WriteRoots.SelectMany(root => RepoScan.Scan(Normalize(root), [sids.Package, sids.CapabilityFor(root)]))
            .Where(entry => !entry.Protected).ToList();

    /// <summary>A write root inside another is refused: the outer root's capability would write the inner one.</summary>
    private static void RefuseNestedWriteRoots(Policy policy)
    {
        foreach (var a in policy.WriteRoots)
            foreach (var b in policy.WriteRoots)
                if (!ReferenceEquals(a, b) && Inside(a, b))
                    throw new AclException("nested_write_roots", $"Write root {Normalize(a)} lies inside write root {Normalize(b)}.");
    }

    /// <summary>The capabilities of the write roots that path lies in (their grant reaches it).</summary>
    private static SecurityIdentifier[] ContainingCapabilities(Policy policy, ConfinementSids sids, string path) =>
        policy.WriteRoots.Where(root => Inside(path, root)).Select(sids.CapabilityFor).ToArray();

    private static RawAcl ReadRootAcl(string root)
    {
        using var handle = AclNative.OpenRoot(root, writeDac: false);
        CheckRoot(handle, root);
        var acl = new RawSecurityDescriptor(AclNative.GetDacl(handle, root), 0).DiscretionaryAcl;
        return acl ?? throw new AclException("null_dacl", $"Root {root} has a NULL DACL (everyone has full access).");
    }

    private static void CheckDenyTargets(Policy policy, ConfinementSids sids)
    {
        foreach (var (path, kind) in DenyPaths(policy))
        {
            var handle = TryOpenDenyTarget(policy, path);
            if (handle is null) continue;
            using (handle)
            {
                var directory = (AclNative.GetAttributes(handle, path) & AclNative.FileAttributeDirectory) != 0;
                var acl = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0).DiscretionaryAcl
                    ?? throw new AclException("null_dacl", $"Denied path {path} has a NULL DACL.");
                var mask = kind == DenyKind.All ? AclRules.DenyAllMask : AclRules.DenyWriteMask;
                // Outside every write root only the package SID's deny is there, and a group grant to every
                // AppContainer may not be judged with it (a package deny does not stop a capability's grant),
                // so a deny path whose own DACL opens the denied access to them is refused.
                if (!policy.WriteRoots.Any(root => Inside(path, root)))
                {
                    var every = AclRules.EveryAppContainerAllowed(acl, kind == DenyKind.All ? AclRules.AnyReadBits | AclRules.WriteOrGenericWriteBits : AclRules.WriteOrGenericWriteBits);
                    if (every is not null)
                        throw new AclException("foreign_access", $"Denied path {path} gives {every.Value} the access it must deny.");
                }
                // The package SID, and the capability of every write root whose grant reaches it.
                foreach (var sid in ContainingCapabilities(policy, sids, path).Prepend(sids.Package))
                    if (!AclRules.HasExplicitDeny(acl, sid, mask, directory))
                        throw new AclException("deny_missing", $"Denied path {path} does not carry its deny ACE for {sid.Value} (run grant).");
            }
        }
    }

    // ---------------------------------------------------------------- revoke

    /// <summary>
    /// Removes every ACE of the policy's capability SIDs and of the package SID from each write root
    /// and everything under it. With includeShared, also the package SID's ACEs on the read roots
    /// and on deny paths outside the roots: those are shared by every policy that names them, so
    /// revoke them only when no other workspace is still granted (re-run grant for the others).
    /// Nothing else in any DACL is touched, so each comes back as it was before grant.
    /// </summary>
    public static AclReport Revoke(Policy policy, ConfinementSids sids, bool includeShared)
    {
        var counts = new Counts();
        foreach (var root in policy.WriteRoots)
            WalkRevoke(Normalize(root), [sids.CapabilityFor(root), sids.Package], counts);
        if (includeShared)
        {
            foreach (var root in policy.ReadRoots) WalkRevoke(Normalize(root), [sids.Package], counts);
            foreach (var (path, _) in DenyPaths(policy))
            {
                if (UnderAnyRoot(policy, path)) continue;
                var handle = TryOpenDenyTarget(policy, path, writeDac: true);
                if (handle is null) continue;
                bool directory;
                using (handle)
                {
                    directory = (AclNative.GetAttributes(handle, path) & AclNative.FileAttributeDirectory) != 0;
                    if (!directory) Strip(handle, path, [sids.Package], counts);
                }
                if (directory) WalkRevoke(path, [sids.Package], counts);
            }
        }
        return counts.Report([]);
    }

    private static void WalkRevoke(string root, SecurityIdentifier[] sids, Counts counts)
    {
        using var rootHandle = AclNative.OpenRoot(root, writeDac: true);
        if ((AclNative.GetAttributes(rootHandle, root) & AclNative.FileAttributeReparsePoint) != 0)
            throw new AclException("reparse_point", $"Root {root} is a reparse point.");
        // The root first: from this moment no job verifies on it, whatever the rest of the walk meets.
        Strip(rootHandle, root, sids, counts);
        var stack = new Stack<Frame>();
        stack.Push(new Frame(rootHandle, root, AclNative.List(rootHandle, root), 0, false, default));
        try
        {
            while (stack.Count > 0)
            {
                var frame = stack.Pop();
                if (frame.Index >= frame.Entries.Count) { if (frame.Own) frame.Handle.Dispose(); continue; }
                var entry = frame.Entries[frame.Index];
                stack.Push(frame with { Index = frame.Index + 1 });
                var path = frame.Path + "\\" + entry.Name;
                if (entry.IsReparsePoint) { counts.ReparsePoints++; continue; }
                var handle = AclNative.OpenChild(frame.Handle, entry.Name, entry.IsDirectory, writeDac: true);
                if (handle is null) { counts.NoAccess++; continue; }
                var keep = false;
                try
                {
                    var attributes = AclNative.GetAttributes(handle, path);
                    if ((attributes & AclNative.FileAttributeReparsePoint) != 0) { counts.ReparsePoints++; continue; }
                    Strip(handle, path, sids, counts);
                    if ((attributes & AclNative.FileAttributeDirectory) != 0)
                    {
                        stack.Push(new Frame(handle, path, AclNative.List(handle, path), 0, true, default));
                        keep = true;
                    }
                }
                finally { if (!keep) handle.Dispose(); }
            }
        }
        finally
        {
            while (stack.Count > 0) { var frame = stack.Pop(); if (frame.Own) frame.Handle.Dispose(); }
        }
    }

    private static void Strip(SafeFileHandle handle, string path, SecurityIdentifier[] sids, Counts counts)
    {
        var descriptor = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0);
        var acl = descriptor.DiscretionaryAcl;
        if (acl is null || AclRules.RemoveAll(acl, sids) == 0) { counts.Unchanged++; return; }
        descriptor.DiscretionaryAcl = acl;
        var bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        AclNative.SetDacl(handle, bytes, (descriptor.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0, path);
        counts.Updated++;
    }

    // ---------------------------------------------------------------- roots and deny paths

    /// <summary>A policy path in the form compared and printed here: '\' separators, no trailing '\'.</summary>
    internal static string Normalize(string path) => path.Replace('/', '\\').TrimEnd('\\');

    private static bool Inside(string child, string root)
    {
        var c = Normalize(child);
        var r = Normalize(root);
        return string.Equals(c, r, StringComparison.OrdinalIgnoreCase) || c.StartsWith(r + "\\", StringComparison.OrdinalIgnoreCase);
    }

    private static bool UnderAnyRoot(Policy policy, string path) =>
        policy.WriteRoots.Concat(policy.ReadRoots).Any(root => Inside(path, root));

    /// <summary>
    /// The root the policy names is a folder, reached with no link on the way (its final path is the
    /// policy's path), on a volume that keeps ACLs, and not a volume root nor inside a repository.
    /// </summary>
    private static void CheckRoot(SafeFileHandle handle, string root)
    {
        if (root.Length <= 2) throw new AclException("volume_root", $"A volume root cannot be a root: {root}.");
        var attributes = AclNative.GetAttributes(handle, root);
        if ((attributes & AclNative.FileAttributeReparsePoint) != 0) throw new AclException("reparse_point", $"Root {root} is a reparse point.");
        if ((attributes & AclNative.FileAttributeDirectory) == 0) throw new AclException("not_a_directory", $"Root {root} is not a folder.");
        var final = Normalize(AclNative.GetFinalPath(handle, root));
        if (!string.Equals(final, root, StringComparison.OrdinalIgnoreCase))
            throw new AclException("root_not_resolved", $"Root {root} resolves to {final} (a link, junction or 8.3 name on the way).");
        if (!AclNative.VolumeKeepsAcls(handle, root))
            throw new AclException("no_acls", $"Root {root} is on a volume without ACLs (FAT, exFAT or a share): nothing there could be confined.");
        if (final.Split('\\').Skip(1).Any(part => RepoScan.IsGitName(part)))
            throw new AclException("root_in_repo", $"Root {root} is inside a repository's .git.");
    }

    private static IEnumerable<(string Path, DenyKind Kind)> DenyPaths(Policy policy) =>
        policy.DenyAllPaths.Select(p => (Normalize(p), DenyKind.All)).Concat(policy.DenyWritePaths.Select(p => (Normalize(p), DenyKind.Write)));

    /// <summary>
    /// Opens a deny path, or returns null when it does not exist and lies under no root (nothing of
    /// ours reaches it). Missing under a root, or a root under it, is refused.
    /// </summary>
    private static SafeFileHandle? TryOpenDenyTarget(Policy policy, string path, bool writeDac = false)
    {
        foreach (var root in policy.WriteRoots.Concat(policy.ReadRoots))
            if (Inside(root, path)) throw new AclException("root_inside_denied", $"Root {Normalize(root)} lies inside denied path {path}.");
        try
        {
            var handle = AclNative.OpenRoot(path, writeDac);
            if ((AclNative.GetAttributes(handle, path) & AclNative.FileAttributeReparsePoint) != 0)
            {
                handle.Dispose();
                throw new AclException("reparse_point", $"Denied path {path} is a reparse point; an ACE on it would not bind its target.");
            }
            return handle;
        }
        catch (AclException error) when (error.Code == "root_missing")
        {
            if (UnderAnyRoot(policy, path))
                throw new AclException("deny_path_missing", $"Denied path {path} lies under a root but does not exist, so it cannot be protected.");
            return null;
        }
    }

    private static DenyKind DenyOf(SafeFileHandle handle, string path, IReadOnlyDictionary<(ulong, string), DenyKind> denyById) =>
        denyById.Count == 0 ? DenyKind.None
        : denyById.TryGetValue(AclNative.GetIdentity(handle, path), out var kind) ? kind : DenyKind.None;

    private sealed class DenyTargets : IDisposable
    {
        public Dictionary<(ulong, string), DenyKind> ById { get; } = new();
        public List<(string Path, SafeFileHandle Handle, DenyKind Kind, bool Directory)> Outside { get; } = [];
        private readonly List<SafeFileHandle> held = [];
        public void Hold(SafeFileHandle handle) => held.Add(handle);
        public void Dispose() { foreach (var handle in held) handle.Dispose(); }
    }

    private static DenyTargets OpenDenyTargets(Policy policy)
    {
        var targets = new DenyTargets();
        try
        {
            foreach (var (path, kind) in DenyPaths(policy))
            {
                var handle = TryOpenDenyTarget(policy, path, writeDac: true);
                if (handle is null) continue;
                targets.Hold(handle);
                var identity = AclNative.GetIdentity(handle, path);
                if (!targets.ById.TryGetValue(identity, out var existing) || kind > existing) targets.ById[identity] = kind;
                if (!UnderAnyRoot(policy, path))
                    targets.Outside.Add((path, handle, kind, (AclNative.GetAttributes(handle, path) & AclNative.FileAttributeDirectory) != 0));
            }
            return targets;
        }
        catch { targets.Dispose(); throw; }
    }
}
