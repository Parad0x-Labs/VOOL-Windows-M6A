using System.Security.AccessControl;
using System.Security.Principal;

namespace VoolConfine.Core;

/// <summary>An ACL step that could not be done or a check that failed. The launcher refuses with 125; the child never starts.</summary>
public sealed class AclException(string code, string message) : Exception(message)
{
    /// <summary>A short stable reason, e.g. "grant_missing", "repo_unprotected", "null_dacl".</summary>
    public string Code { get; } = code;
}

/// <summary>
/// The confinement's ACEs and how one is put into, found in, or taken out of a DACL.
/// Masks are specific rights only (no GENERIC_* bits), so a copy on a child means exactly what the
/// original means on its folder.
/// </summary>
public static class AclRules
{
    // FILE_GENERIC_READ 0x120089 | FILE_GENERIC_WRITE 0x120116 | FILE_GENERIC_EXECUTE 0x1200A0 | DELETE 0x10000.
    // Never FILE_DELETE_CHILD (0x40): with it a job could delete or rename a protected .git through
    // its parent, whatever the .git's own ACL says. Never WRITE_DAC or WRITE_OWNER: a job cannot edit an ACL.
    public const int ReadWriteMask = 0x001301BF;
    // FILE_GENERIC_READ | FILE_GENERIC_EXECUTE.
    public const int ReadExecuteMask = 0x001200A9;
    // FILE_WRITE_DATA/ADD_FILE 0x2 | FILE_APPEND_DATA/ADD_SUBDIRECTORY 0x4 | FILE_WRITE_EA 0x10 |
    // FILE_DELETE_CHILD 0x40 | FILE_WRITE_ATTRIBUTES 0x100 | DELETE 0x10000 | WRITE_DAC 0x40000 | WRITE_OWNER 0x80000.
    public const int DenyWriteMask = 0x000D0156;
    // FILE_ALL_ACCESS.
    public const int DenyAllMask = 0x001F01FF;
    // Every right that changes an object, its name, its attributes or its security.
    internal const int AnyWriteBits = DenyWriteMask;
    // FILE_READ_DATA 0x1 | FILE_READ_EA 0x8 | FILE_EXECUTE 0x20 | FILE_READ_ATTRIBUTES 0x80 | READ_CONTROL 0x20000,
    // with GENERIC_READ (0x80000000), GENERIC_EXECUTE (0x20000000) and GENERIC_ALL (0x10000000).
    internal const int AnyReadBits = 0x000200A9 | unchecked((int)0x80000000) | 0x20000000 | 0x10000000;
    // The same, with GENERIC_WRITE (0x40000000) and GENERIC_ALL (0x10000000), which an ACE may carry unmapped.
    internal const int WriteOrGenericWriteBits = AnyWriteBits | 0x50000000;

    internal const AceFlags FolderInherit = AceFlags.ObjectInherit | AceFlags.ContainerInherit;

    /// <summary>Where an ACE goes, so the DACL stays in canonical order and every existing deny still wins.</summary>
    internal enum Placement
    {
        /// <summary>Explicit deny: first in the DACL.</summary>
        ExplicitDeny,
        /// <summary>Explicit allow: after every explicit ACE, before the first inherited one.</summary>
        ExplicitAllow,
        /// <summary>A propagated (inherited-marked) allow: last in the DACL, the weakest place.</summary>
        InheritedAllow,
    }

    internal readonly record struct Want(SecurityIdentifier Sid, AceQualifier Qualifier, int Mask, AceFlags Flags, Placement Placement);

    internal static Want ExplicitAllow(SecurityIdentifier sid, int mask, bool directory) =>
        new(sid, AceQualifier.AccessAllowed, mask, directory ? FolderInherit : AceFlags.None, Placement.ExplicitAllow);

    internal static Want PropagatedAllow(SecurityIdentifier sid, int mask, bool directory) =>
        new(sid, AceQualifier.AccessAllowed, mask, AceFlags.Inherited | (directory ? FolderInherit : AceFlags.None), Placement.InheritedAllow);

    internal static Want ExplicitDeny(SecurityIdentifier sid, int mask, bool directory) =>
        new(sid, AceQualifier.AccessDenied, mask, directory ? FolderInherit : AceFlags.None, Placement.ExplicitDeny);

    private static bool Same(GenericAce ace, SecurityIdentifier sid, AceQualifier qualifier, int mask) =>
        ace is CommonAce common && common.SecurityIdentifier == sid && common.AceQualifier == qualifier && common.AccessMask == mask;

    private static bool IsInherited(GenericAce ace) => (ace.AceFlags & AceFlags.Inherited) != 0;

    private static bool IsExplicitDeny(GenericAce ace) =>
        !IsInherited(ace) && ace is KnownAce known && known.AceType is AceType.AccessDenied or AceType.AccessDeniedObject or AceType.AccessDeniedCallback or AceType.AccessDeniedCallbackObject;

    private static bool PlacedRight(RawAcl acl, int index, Placement placement)
    {
        switch (placement)
        {
            case Placement.ExplicitDeny:
                for (var i = 0; i < index; i++) if (!IsExplicitDeny(acl[i])) return false;
                return true;
            case Placement.ExplicitAllow:
                for (var i = 0; i < index; i++) if (IsInherited(acl[i])) return false;
                return true;
            default:
                for (var i = index + 1; i < acl.Count; i++) if (!IsInherited(acl[i])) return false;
                return true;
        }
    }

    /// <summary>
    /// Makes the DACL hold exactly one ACE for (SID, qualifier, mask), with the wanted flags, in its
    /// place. Returns false when it was already so (the DACL is then left byte for byte as it was).
    /// </summary>
    internal static bool Ensure(RawAcl acl, Want want)
    {
        var matches = new List<int>();
        for (var i = 0; i < acl.Count; i++) if (Same(acl[i], want.Sid, want.Qualifier, want.Mask)) matches.Add(i);
        if (matches.Count == 1 && acl[matches[0]].AceFlags == want.Flags && PlacedRight(acl, matches[0], want.Placement)) return false;
        for (var i = matches.Count - 1; i >= 0; i--) acl.RemoveAce(matches[i]);
        var index = want.Placement switch
        {
            Placement.ExplicitDeny => 0,
            Placement.ExplicitAllow => FirstInherited(acl),
            _ => acl.Count,
        };
        acl.InsertAce(index, new CommonAce(want.Flags, want.Qualifier, want.Mask, want.Sid, false, null));
        return true;
    }

    private static int FirstInherited(RawAcl acl)
    {
        for (var i = 0; i < acl.Count; i++) if (IsInherited(acl[i])) return i;
        return acl.Count;
    }

    /// <summary>Removes every ACE whose SID is one of sids. Returns how many were removed.</summary>
    internal static int RemoveAll(RawAcl acl, IReadOnlyCollection<SecurityIdentifier> sids)
    {
        var removed = 0;
        for (var i = acl.Count - 1; i >= 0; i--)
        {
            if (acl[i] is KnownAce known && sids.Contains(known.SecurityIdentifier)) { acl.RemoveAce(i); removed++; }
        }
        return removed;
    }

    /// <summary>
    /// True when the DACL holds a deny ACE for sid, not inherited and not inherit-only, covering
    /// every bit of mask; on a folder it must also carry object and container inherit, so whatever
    /// is made inside later is denied too.
    /// </summary>
    public static bool HasExplicitDeny(RawAcl acl, SecurityIdentifier sid, int mask, bool directory)
    {
        foreach (GenericAce ace in acl)
        {
            if (ace is not CommonAce common || common.AceQualifier != AceQualifier.AccessDenied) continue;
            if (common.SecurityIdentifier != sid || (common.AccessMask & mask) != mask) continue;
            if ((ace.AceFlags & (AceFlags.Inherited | AceFlags.InheritOnly)) != 0) continue;
            if (directory && (ace.AceFlags & FolderInherit) != FolderInherit) continue;
            if (directory && (ace.AceFlags & AceFlags.NoPropagateInherit) != 0) continue;
            return true;
        }
        return false;
    }

    /// <summary>True when the DACL holds our explicit allow for sid exactly: the mask, and on a folder OI|CI with nothing else.</summary>
    public static bool HasExplicitAllow(RawAcl acl, SecurityIdentifier sid, int mask, bool directory)
    {
        var flags = directory ? FolderInherit : AceFlags.None;
        foreach (GenericAce ace in acl)
            if (ace is CommonAce common && common.AceQualifier == AceQualifier.AccessAllowed && common.SecurityIdentifier == sid
                && common.AccessMask == mask && ace.AceFlags == flags) return true;
        return false;
    }

    /// <summary>
    /// The first AppContainer SID (package S-1-15-2-*, ALL APPLICATION PACKAGES and ALL RESTRICTED
    /// APP PACKAGES included, or capability S-1-15-3-*) not in allowed that an allow ACE (explicit or
    /// inherited, not inherit-only) gives any write right; null when there is none.
    /// </summary>
    public static SecurityIdentifier? ForeignWriter(RawAcl acl, IReadOnlyCollection<SecurityIdentifier> allowed)
    {
        foreach (GenericAce ace in acl)
        {
            if (ace is not CommonAce common || common.AceQualifier != AceQualifier.AccessAllowed) continue;
            if ((ace.AceFlags & AceFlags.InheritOnly) != 0 || (common.AccessMask & WriteOrGenericWriteBits) == 0) continue;
            var sid = common.SecurityIdentifier;
            var text = sid.Value;
            if (!text.StartsWith("S-1-15-2-", StringComparison.Ordinal) && !text.StartsWith("S-1-15-3-", StringComparison.Ordinal)) continue;
            if (!allowed.Contains(sid)) return sid;
        }
        return null;
    }

    /// <summary>ALL APPLICATION PACKAGES and ALL RESTRICTED APP PACKAGES: every job's token matches both.</summary>
    internal static readonly SecurityIdentifier[] EveryAppContainer =
        [new SecurityIdentifier("S-1-15-2-1"), new SecurityIdentifier("S-1-15-2-2")];

    /// <summary>
    /// The first SID of sids, or ALL APPLICATION PACKAGES / ALL RESTRICTED APP PACKAGES, that an
    /// allow ACE gives any of bits (GENERIC_* included), INHERIT-ONLY ACEs counted too: on a folder
    /// they are what files and folders made inside it later receive. Null when there is none.
    /// This is the test a protected object (a repository, a denied path) must pass: a deny ACE for
    /// an AppContainer SID does not stop an allow to the package SID's capabilities or to every
    /// AppContainer (measured, windows-broker runs 41901543557 and 41902677009), so protection is
    /// the absence of any allow, never a deny.
    /// </summary>
    public static SecurityIdentifier? AllowedTo(RawAcl acl, IEnumerable<SecurityIdentifier> sids, int bits)
    {
        var watched = sids.Concat(EveryAppContainer).ToHashSet();
        foreach (GenericAce ace in acl)
        {
            if (ace is not CommonAce common || common.AceQualifier != AceQualifier.AccessAllowed) continue;
            if ((common.AccessMask & bits) == 0) continue;
            if (watched.Contains(common.SecurityIdentifier)) return common.SecurityIdentifier;
        }
        return null;
    }

    /// <summary>
    /// Removes every allow ACE for one of sids (ours only: the package SID and our capabilities)
    /// that carries any of bits. Returns true when one was removed.
    /// </summary>
    internal static bool RemoveAllows(RawAcl acl, IReadOnlyCollection<SecurityIdentifier> sids, int bits)
    {
        var removed = false;
        for (var i = acl.Count - 1; i >= 0; i--)
        {
            if (acl[i] is CommonAce common && common.AceQualifier == AceQualifier.AccessAllowed
                && (common.AccessMask & bits) != 0 && sids.Contains(common.SecurityIdentifier))
            {
                acl.RemoveAce(i);
                removed = true;
            }
        }
        return removed;
    }

    /// <summary>Every bit an ACE may carry: any allow at all.</summary>
    internal const int AnyBits = unchecked((int)0xFFFFFFFF);

    /// <summary>
    /// ALL APPLICATION PACKAGES (S-1-15-2-1) or ALL RESTRICTED APP PACKAGES (S-1-15-2-2) when an
    /// allow ACE (not inherit-only) gives it any of bits; null otherwise.
    /// </summary>
    public static SecurityIdentifier? EveryAppContainerAllowed(RawAcl acl, int bits)
    {
        foreach (GenericAce ace in acl)
        {
            if (ace is not CommonAce common || common.AceQualifier != AceQualifier.AccessAllowed) continue;
            if ((ace.AceFlags & AceFlags.InheritOnly) != 0 || (common.AccessMask & bits) == 0) continue;
            if (common.SecurityIdentifier.Value is "S-1-15-2-1" or "S-1-15-2-2") return common.SecurityIdentifier;
        }
        return null;
    }

    /// <summary>The union of every allow ACE's mask for sid (explicit or inherited, inherit-only excluded).</summary>
    public static int AllowedBits(RawAcl acl, SecurityIdentifier sid)
    {
        var bits = 0;
        foreach (GenericAce ace in acl)
            if (ace is CommonAce common && common.AceQualifier == AceQualifier.AccessAllowed && common.SecurityIdentifier == sid
                && (ace.AceFlags & AceFlags.InheritOnly) == 0) bits |= common.AccessMask;
        return bits;
    }
}
