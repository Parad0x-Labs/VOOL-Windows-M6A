using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;
using VoolConfine.Core;
using Xunit;

namespace VoolConfine.Tests;

/// <summary>
/// A real kernel check of the ACL code with no launcher: the test makes a lowbox (AppContainer)
/// token from its own token, with a test package SID and the capability SIDs it names, and
/// impersonates it on one thread while it opens files with CreateFileW. So every "the job can" /
/// "the job cannot" below is the kernel's own access check, AppContainer rules included.
/// </summary>
internal sealed class JobToken : IDisposable
{
    private readonly nint impersonation;

    private JobToken(nint impersonation) => this.impersonation = impersonation;

    public static JobToken Create(SecurityIdentifier package, params SecurityIdentifier[] capabilities)
    {
        const uint MaximumAllowed = 0x02000000;
        if (!AclTest.OpenProcessToken(AclTest.GetCurrentProcess(), MaximumAllowed, out var own))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenProcessToken failed.");
        var memory = new List<nint>();
        try
        {
            var packageSid = AclTest.ToNative(package, memory);
            var caps = capabilities.Select(sid => new AclTest.SidAndAttributes { Sid = AclTest.ToNative(sid, memory), Attributes = 0x4 /* SE_GROUP_ENABLED */ }).ToArray();
            var attributes = new AclTest.ObjectAttributes { Length = Marshal.SizeOf<AclTest.ObjectAttributes>() };
            var status = AclTest.NtCreateLowBoxToken(out var lowbox, own, MaximumAllowed, ref attributes, packageSid,
                (uint)caps.Length, caps.Length == 0 ? null : caps, 0, 0);
            if (status < 0) throw new InvalidOperationException($"NtCreateLowBoxToken failed: NTSTATUS 0x{unchecked((uint)status):x8}.");
            try
            {
                if (!AclTest.DuplicateTokenEx(lowbox, MaximumAllowed, 0, 2 /* SecurityImpersonation */, 2 /* TokenImpersonation */, out var impersonation))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "DuplicateTokenEx failed.");
                return new JobToken(impersonation);
            }
            finally { AclTest.CloseHandle(lowbox); }
        }
        finally
        {
            AclTest.CloseHandle(own);
            foreach (var block in memory) Marshal.FreeHGlobal(block);
        }
    }

    /// <summary>Runs op on a fresh thread that impersonates the job token. Returns the Win32 error (0 = it worked).</summary>
    public int Run(Func<int> op)
    {
        var result = -1;
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                if (!AclTest.SetThreadToken(0, impersonation)) throw new Win32Exception(Marshal.GetLastWin32Error(), "SetThreadToken failed.");
                try { result = op(); }
                finally
                {
                    if (!AclTest.RevertToSelf()) Environment.FailFast("RevertToSelf failed; this thread must not run on.");
                }
            }
            catch (Exception error) { failure = error; }
        });
        thread.Start();
        thread.Join();
        if (failure is not null) throw new InvalidOperationException("The impersonating thread failed.", failure);
        return result;
    }

    public int Open(string path, uint access, uint disposition = AclTest.OpenExisting) => Run(() => AclTest.TryOpen(path, access, disposition));
    public bool CanRead(string path) => Open(path, AclTest.FileReadData) == 0;
    public bool CanWrite(string path) => Open(path, AclTest.FileWriteData) == 0;
    public bool CanAppend(string path) => Open(path, AclTest.FileAppendData) == 0;
    public bool CanDelete(string path) => Open(path, AclTest.Delete) == 0;
    public bool CanWriteDac(string path) => Open(path, AclTest.WriteDac) == 0;
    public bool CanReadControl(string path) => Open(path, AclTest.ReadControl) == 0;
    public bool CanCreateFile(string path) => Open(path, AclTest.FileWriteData, AclTest.CreateNew) == 0;
    public bool CanCreateDirectory(string path) => Run(() => AclTest.CreateDirectoryW(path, 0) ? 0 : Marshal.GetLastWin32Error()) == 0;
    public bool CanRename(string from, string to) => Run(() => AclTest.MoveFileExW(from, to, 0) ? 0 : Marshal.GetLastWin32Error()) == 0;

    public void Dispose() => AclTest.CloseHandle(impersonation);
}

internal static class AclTest
{
    public const uint FileReadData = 0x1;
    public const uint FileWriteData = 0x2;
    public const uint FileAppendData = 0x4;
    public const uint Delete = 0x00010000;
    public const uint ReadControl = 0x00020000;
    public const uint WriteDac = 0x00040000;
    public const uint OpenExisting = 3;
    public const uint CreateNew = 1;
    private const uint BackupSemantics = 0x02000000;
    private const uint OpenReparsePoint = 0x00200000;

    [StructLayout(LayoutKind.Sequential)]
    public struct SidAndAttributes { public nint Sid; public uint Attributes; }

    [StructLayout(LayoutKind.Sequential)]
    public struct ObjectAttributes { public int Length; public nint RootDirectory; public nint ObjectName; public uint Attributes; public nint SecurityDescriptor; public nint SecurityQualityOfService; }

    [DllImport("kernel32.dll")] public static extern nint GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool CloseHandle(nint handle);
    [DllImport("advapi32.dll", SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool OpenProcessToken(nint process, uint access, out nint token);
    [DllImport("advapi32.dll", SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool DuplicateTokenEx(nint token, uint access, nint attributes, int level, int type, out nint newToken);
    [DllImport("advapi32.dll", SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool SetThreadToken(nint thread, nint token);
    [DllImport("advapi32.dll", SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool RevertToSelf();
    [DllImport("ntdll.dll")] public static extern int NtCreateLowBoxToken(out nint token, nint existing, uint access, ref ObjectAttributes attributes,
        nint packageSid, uint capabilityCount, SidAndAttributes[]? capabilities, uint handleCount, nint handles);
    [DllImport("userenv.dll", CharSet = CharSet.Unicode)] private static extern int DeriveAppContainerSidFromAppContainerName(string name, out nint sid);
    [DllImport("advapi32.dll")] private static extern nint FreeSid(nint sid);
    [DllImport("kernelbase.dll", CharSet = CharSet.Unicode, SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DeriveCapabilitySidsFromName(string name, out nint groupSids, out uint groupCount, out nint sids, out uint sidCount);
    [DllImport("kernel32.dll")] private static extern nint LocalFree(nint memory);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string name, uint access, uint share, nint security, uint creation, uint flags, nint template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool CreateDirectoryW(string path, nint security);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] public static extern bool MoveFileExW(string from, string to, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)][return: MarshalAs(UnmanagedType.Bool)] private static extern bool CreateHardLinkW(string link, string existing, nint security);

    public static nint ToNative(SecurityIdentifier sid, List<nint> memory)
    {
        var bytes = new byte[sid.BinaryLength];
        sid.GetBinaryForm(bytes, 0);
        var block = Marshal.AllocHGlobal(bytes.Length);
        memory.Add(block);
        Marshal.Copy(bytes, 0, block, bytes.Length);
        return block;
    }

    /// <summary>Opens path (a folder too; a link itself, not its target, only when the caller asks) and closes it at once. Returns the Win32 error, 0 when it opened.</summary>
    public static int TryOpen(string path, uint access, uint disposition, bool openLink = false)
    {
        using var handle = CreateFileW(path, access, 1 | 2 | 4, 0, disposition, BackupSemantics | (openLink ? OpenReparsePoint : 0), 0);
        return handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
    }

    /// <summary>A package SID from a name, as the launcher's own derivation does (userenv).</summary>
    public static SecurityIdentifier PackageSid(string name)
    {
        var hr = DeriveAppContainerSidFromAppContainerName(name, out var sid);
        if (hr != 0) throw new InvalidOperationException($"DeriveAppContainerSidFromAppContainerName failed: HRESULT 0x{hr:x8}.");
        try { return new SecurityIdentifier(sid); }
        finally { FreeSid(sid); }
    }

    /// <summary>The capability SID for a name (S-1-15-3-...), from DeriveCapabilitySidsFromName.</summary>
    public static SecurityIdentifier CapabilitySid(string name)
    {
        if (!DeriveCapabilitySidsFromName(name, out var groups, out var groupCount, out var sids, out var sidCount))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "DeriveCapabilitySidsFromName failed.");
        try
        {
            if (sidCount < 1) throw new InvalidOperationException("DeriveCapabilitySidsFromName returned no capability SID.");
            return new SecurityIdentifier(Marshal.ReadIntPtr(sids));
        }
        finally
        {
            for (var i = 0; i < groupCount; i++) LocalFree(Marshal.ReadIntPtr(groups, i * nint.Size));
            for (var i = 0; i < sidCount; i++) LocalFree(Marshal.ReadIntPtr(sids, i * nint.Size));
            LocalFree(groups);
            LocalFree(sids);
        }
    }

    public static void HardLink(string link, string existing)
    {
        if (!CreateHardLinkW(link, existing, 0)) throw new Win32Exception(Marshal.GetLastWin32Error(), $"CreateHardLink {link} failed.");
    }

    /// <summary>The DACL of path as SDDL (D: part only, control flags included), read by handle.</summary>
    public static string Sddl(string path)
    {
        using var handle = AclNative.OpenRoot(path, writeDac: false);
        return new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0).GetSddlForm(AccessControlSections.Access);
    }

    /// <summary>Replaces path's DACL with the one in sddl (a "D:..." string), by handle.</summary>
    public static void SetSddl(string path, string sddl)
    {
        using var handle = AclNative.OpenRoot(path, writeDac: true);
        var descriptor = new RawSecurityDescriptor(sddl);
        var bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        AclNative.SetDacl(handle, bytes, (descriptor.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0, path);
    }

    /// <summary>Puts one ACE first in path's DACL, by handle (to tamper with a grant in a test).</summary>
    public static void AddAce(string path, AceQualifier qualifier, int mask, SecurityIdentifier sid, AceFlags flags)
    {
        using var handle = AclNative.OpenRoot(path, writeDac: true);
        var descriptor = new RawSecurityDescriptor(AclNative.GetDacl(handle, path), 0);
        var acl = descriptor.DiscretionaryAcl!;
        acl.InsertAce(0, new CommonAce(flags, qualifier, mask, sid, false, null));
        descriptor.DiscretionaryAcl = acl;
        var bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        AclNative.SetDacl(handle, bytes, (descriptor.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0, path);
    }

    /// <summary>Every path under root (root included), not through reparse points, with its DACL.</summary>
    public static SortedDictionary<string, string> SnapshotTree(string root)
    {
        var all = new SortedDictionary<string, string>(StringComparer.Ordinal);
        var pending = new Stack<string>();
        pending.Push(root);
        while (pending.Count > 0)
        {
            var path = pending.Pop();
            all[path] = Sddl(path);
            var info = new DirectoryInfo(path);
            if ((info.Attributes & FileAttributes.Directory) == 0 || (info.Attributes & FileAttributes.ReparsePoint) != 0) continue;
            foreach (var entry in info.EnumerateFileSystemInfos())
            {
                if ((entry.Attributes & FileAttributes.ReparsePoint) != 0) { all[entry.FullName] = Sddl(entry.FullName); continue; }
                if ((entry.Attributes & FileAttributes.Directory) != 0) pending.Push(entry.FullName);
                else all[entry.FullName] = Sddl(entry.FullName);
            }
        }
        return all;
    }

    /// <summary>A directory junction, made by cmd's mklink /J (no privilege needed).</summary>
    public static void Junction(string link, string target)
    {
        using var mklink = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("cmd.exe")
        {
            ArgumentList = { "/d", "/c", "mklink", "/J", link, target }, UseShellExecute = false,
            RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
        })!;
        var output = mklink.StandardOutput.ReadToEnd() + mklink.StandardError.ReadToEnd();
        mklink.WaitForExit();
        if (mklink.ExitCode != 0) throw new InvalidOperationException($"mklink /J failed ({mklink.ExitCode}): {output}");
    }
}

/// <summary>A workspace, an outside folder, the SIDs and a policy for one test; everything is removed after.</summary>
internal sealed class AclFixture : IDisposable
{
    public string Base { get; }
    public string Root { get; }
    public string Outside { get; }
    public SecurityIdentifier Package { get; }
    public SecurityIdentifier Capability { get; }
    private readonly List<string> extraWriteRoots = [];
    private readonly List<string> readRoots = [];
    private readonly List<string> denyWrite = [];
    private readonly List<string> denyAll = [];
    public bool DenyWriteGit { get; set; } = true;

    public AclFixture()
    {
        Base = Path.Combine(Path.GetTempPath(), "vool-confine-acl-" + Guid.NewGuid().ToString("N"));
        Root = Path.Combine(Base, "workspace");
        Outside = Path.Combine(Base, "outside");
        Directory.CreateDirectory(Root);
        Directory.CreateDirectory(Outside);
        // Resolve once (TEMP may hold an 8.3 name, which the policy must not).
        Root = Acl.Normalize(AclTestPaths.Final(Root));
        Outside = Acl.Normalize(AclTestPaths.Final(Outside));
        Base = Path.GetDirectoryName(Root)!;
        // A fresh package name per test, so no test can see another's ACEs.
        Package = AclTest.PackageSid("VOOL.Confine.Tests." + Guid.NewGuid().ToString("N")[..16]);
        Capability = AclTest.CapabilitySid(Identity.RootCapabilityName(Root));
    }

    public string In(params string[] parts) => Path.Combine(new[] { Root }.Concat(parts).ToArray());
    public string Out(params string[] parts) => Path.Combine(new[] { Outside }.Concat(parts).ToArray());

    public void AddWriteRoot(string root) => extraWriteRoots.Add(root);
    public void AddReadRoot(string root) => readRoots.Add(root);
    public void AddDenyWrite(string path) => denyWrite.Add(path);
    public void AddDenyAll(string path) => denyAll.Add(path);

    public Policy MakePolicy()
    {
        var json = JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["schema"] = Policy.Schema,
            ["write_roots"] = new[] { Root }.Concat(extraWriteRoots).ToArray(),
            ["read_roots"] = readRoots.ToArray(),
            ["deny_write_paths"] = denyWrite.ToArray(),
            ["deny_all_paths"] = denyAll.ToArray(),
            ["deny_write_git"] = DenyWriteGit,
            ["temp_dir"] = Path.Combine(Root, ".vool-tmp"),
            ["status_path"] = Path.Combine(Outside, "status", "status.json"),
        });
        return Policy.Parse(json);
    }

    public ConfinementSids Sids()
    {
        var caps = new Dictionary<string, SecurityIdentifier>(StringComparer.Ordinal) { [Root] = Capability };
        foreach (var root in extraWriteRoots) caps[root] = AclTest.CapabilitySid(Identity.RootCapabilityName(root));
        return new ConfinementSids(Package, caps);
    }

    public AclReport Grant() => Acl.Grant(MakePolicy(), Sids());

    /// <summary>The job's token: the package SID plus the capability of this fixture's own root.</summary>
    public JobToken Job() => JobToken.Create(Package, Capability);

    public void Dispose()
    {
        try { Acl.Revoke(MakePolicy(), Sids(), includeShared: true); } catch (Exception) { /* best effort: the folder goes next */ }
        try { Directory.Delete(Base, recursive: true); } catch (Exception) { /* TEMP is cleaned by the runner */ }
    }
}

internal static class AclTestPaths
{
    public static string Final(string path)
    {
        using var handle = AclNative.OpenRoot(path, writeDac: false);
        return AclNative.GetFinalPath(handle, path);
    }
}
