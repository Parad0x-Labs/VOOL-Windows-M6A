using Microsoft.Win32.SafeHandles;
using System.Security.AccessControl;
using System.Security.Principal;

namespace VoolConfine.Core;

/// <summary>What kind of repository layout an entry is.</summary>
public enum RepoKind
{
    /// <summary>A folder named .git (or a name Windows treats as .git).</summary>
    GitDirectory,
    /// <summary>A file named .git: a "gitdir:" pointer, as worktrees and submodules use.</summary>
    GitFile,
    /// <summary>A junction, symlink or other reparse point named .git. No ACE on it binds what it points to.</summary>
    GitLink,
    /// <summary>A folder holding HEAD, objects and refs: git treats it as a bare repository with no .git name at all.</summary>
    BareRepository,
}

/// <summary>One repository layout under a write root, and whether it carries the confinement's deny-write ACE.</summary>
public sealed record RepoEntry(string Path, RepoKind Kind, bool Protected);

/// <summary>
/// Finds every repository layout under a root (docs/WINDOWS_CONFINEMENT.md section 3, deny_write_git).
/// Used three ways: by <see cref="Acl.Grant"/> to know where to put the deny ACE, by
/// <see cref="Acl.Verify"/> before a job (every layout must carry it), and after a job (one that
/// does not carry it was made by the job: a nested git init, a planted hook, a bare repo).
/// Folders are walked by handle and reparse points are never followed. A repository folder is not
/// descended into: what is inside it is covered by its own deny ACE.
/// </summary>
public static class RepoScan
{
    /// <summary>
    /// True for a name git on Windows would take as ".git": compared without case, and with the
    /// trailing dots and spaces Win32 strips (".git.", ".git " are made through \\?\ paths and
    /// opened as ".git" by Win32 callers); or an 8.3 name GIT~1.
    /// </summary>
    public static bool IsGitName(string name, string shortName = "") =>
        string.Equals(name.TrimEnd('.', ' '), ".git", StringComparison.OrdinalIgnoreCase)
        || string.Equals(shortName, "GIT~1", StringComparison.OrdinalIgnoreCase);

    /// <summary>git's own test for a bare repository folder: a HEAD file, an objects folder and a refs folder.</summary>
    internal static bool IsBareShape(IReadOnlyList<AclNative.Entry> entries)
    {
        bool head = false, objects = false, refs = false;
        foreach (var entry in entries)
        {
            if (entry.IsReparsePoint) continue;
            if (!entry.IsDirectory && string.Equals(entry.Name, "HEAD", StringComparison.OrdinalIgnoreCase)) head = true;
            else if (entry.IsDirectory && string.Equals(entry.Name, "objects", StringComparison.OrdinalIgnoreCase)) objects = true;
            else if (entry.IsDirectory && string.Equals(entry.Name, "refs", StringComparison.OrdinalIgnoreCase)) refs = true;
        }
        return head && objects && refs;
    }

    /// <summary>
    /// Every repository layout under root, each marked protected when its DACL holds an explicit
    /// deny ACE for each of denySids (the package SID and the root's capability) covering every write
    /// right (and, on a folder, inherited by files and folders made inside it), AND no allow ACE gives
    /// any of denySids, ALL APPLICATION PACKAGES or ALL RESTRICTED APP PACKAGES a write right.
    /// A link named .git is never protected.
    /// Any failure to open or list a folder throws: an unlisted folder could hide a repository.
    /// </summary>
    public static IReadOnlyList<RepoEntry> Scan(string root, IReadOnlyList<SecurityIdentifier> denySids)
    {
        var found = new List<RepoEntry>();
        using var rootHandle = AclNative.OpenRoot(root, writeDac: false);
        if ((AclNative.GetAttributes(rootHandle, root) & AclNative.FileAttributeReparsePoint) != 0)
            throw new AclException("reparse_point", $"Root {root} is a reparse point.");
        var rootEntries = AclNative.List(rootHandle, root);
        if (IsBareShape(rootEntries))
            found.Add(new RepoEntry(root, RepoKind.BareRepository, IsProtected(rootHandle, root, true, denySids)));
        else
            Walk(rootHandle, root, rootEntries, denySids, found);
        return found;
    }

    private static void Walk(SafeFileHandle top, string topPath, List<AclNative.Entry> topEntries,
        IReadOnlyList<SecurityIdentifier> denySids, List<RepoEntry> found)
    {
        // An explicit stack, so a deep tree cannot overflow the thread's stack.
        var stack = new Stack<Frame>();
        stack.Push(new Frame(top, topPath, topEntries, 0, false));
        try
        {
            while (stack.Count > 0)
            {
                var frame = stack.Pop();
                if (frame.Index >= frame.Entries.Count)
                {
                    if (frame.Own) frame.Handle.Dispose();
                    continue;
                }
                var entry = frame.Entries[frame.Index];
                stack.Push(frame with { Index = frame.Index + 1 });
                var path = frame.Path + "\\" + entry.Name;
                if (IsGitName(entry.Name, entry.ShortName))
                {
                    if (entry.IsReparsePoint) { found.Add(new RepoEntry(path, RepoKind.GitLink, false)); continue; }
                    using var handle = OpenOrThrow(frame.Handle, entry, path);
                    found.Add(new RepoEntry(path, entry.IsDirectory ? RepoKind.GitDirectory : RepoKind.GitFile,
                        IsProtected(handle, path, entry.IsDirectory, denySids)));
                    continue;
                }
                if (!entry.IsDirectory || entry.IsReparsePoint) continue;
                var child = OpenOrThrow(frame.Handle, entry, path);
                List<AclNative.Entry> entries;
                try { entries = AclNative.List(child, path); }
                catch { child.Dispose(); throw; }
                if (IsBareShape(entries))
                {
                    try { found.Add(new RepoEntry(path, RepoKind.BareRepository, IsProtected(child, path, true, denySids))); }
                    finally { child.Dispose(); }
                    continue;
                }
                stack.Push(new Frame(child, path, entries, 0, true));
            }
        }
        finally
        {
            while (stack.Count > 0) { var frame = stack.Pop(); if (frame.Own) frame.Handle.Dispose(); }
        }
    }

    private readonly record struct Frame(SafeFileHandle Handle, string Path, List<AclNative.Entry> Entries, int Index, bool Own);

    private static SafeFileHandle OpenOrThrow(SafeFileHandle parent, AclNative.Entry entry, string path) =>
        AclNative.OpenChild(parent, entry.Name, entry.IsDirectory, writeDac: false)
        ?? throw new AclException("access_denied", $"Cannot open {path} to check it (access denied).");

    internal static bool IsProtected(SafeFileHandle handle, string path, bool directory, IReadOnlyList<SecurityIdentifier> denySids)
    {
        var descriptor = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0);
        var acl = descriptor.DiscretionaryAcl;
        if (acl is null || denySids.Count == 0) return false;
        RawAcl dacl = acl;
        // The deny ACEs are the marker; what protects it is that no SID of a job's token (the
        // package SID, the root's capability, every AppContainer) is allowed any write right, by an
        // ACE here or by an inherit-only one that what is made inside later would receive.
        return denySids.All(sid => AclRules.HasExplicitDeny(dacl, sid, AclRules.DenyWriteMask, directory))
            && AclRules.AllowedTo(dacl, denySids, AclRules.WriteOrGenericWriteBits) is null;
    }
}
