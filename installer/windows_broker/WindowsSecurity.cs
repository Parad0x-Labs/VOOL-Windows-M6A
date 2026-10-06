using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;

namespace Vool.WindowsBroker;

internal sealed class AppContainerSession : IDisposable
{
    private bool disposed;

    private AppContainerSession(string name, IntPtr sidPointer)
    {
        Name = name;
        SidPointer = sidPointer;
        Sid = new SecurityIdentifier(NativeMethods.SidToString(sidPointer));
    }

    internal string Name { get; }
    internal IntPtr SidPointer { get; }
    internal SecurityIdentifier Sid { get; }

    internal static AppContainerSession Create(
        string pluginId = "probe",
        string pluginVersion = "probe",
        string pluginDigest = "probe",
        string operationId = "probe")
    {
        NativeMethods.RequireExport("userenv.dll", "CreateAppContainerProfile");
        NativeMethods.RequireExport("userenv.dll", "DeleteAppContainerProfile");
        var identity = string.Join(
            "\0",
            "vool-windows-broker-v1",
            pluginId,
            pluginVersion,
            pluginDigest,
            operationId);
        var identityHash = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant();
        var name = $"Vool.Broker.v1.{identityHash[..32]}";
        var result = NativeMethods.CreateAppContainerProfile(
            name,
            "VOOL native execution broker",
            "Ephemeral AppContainer for one untrusted process execution.",
            IntPtr.Zero,
            0,
            out var sidPointer);

        if (result < 0 || sidPointer == IntPtr.Zero)
        {
            if (sidPointer != IntPtr.Zero)
                NativeMethods.FreeSid(sidPointer);
            throw new BrokerSecurityException(
                $"CreateAppContainerProfile failed with HRESULT 0x{result:X8}.");
        }

        try
        {
            return new AppContainerSession(name, sidPointer);
        }
        catch
        {
            NativeMethods.DeleteAppContainerProfile(name);
            NativeMethods.FreeSid(sidPointer);
            throw;
        }
    }

    public void Dispose()
    {
        if (disposed)
            return;

        disposed = true;
        Exception? failure = null;
        try
        {
            var result = NativeMethods.DeleteAppContainerProfile(Name);
            if (result < 0)
                failure = new BrokerSecurityException(
                    $"DeleteAppContainerProfile failed with HRESULT 0x{result:X8}.");
        }
        finally
        {
            NativeMethods.FreeSid(SidPointer);
        }

        if (failure is not null)
            throw failure;
    }
}

internal static class TokenInspector
{
    internal static TokenSecuritySnapshot Inspect(SafeNativeHandle token)
    {
        var isAppContainer = ReadBool(token, NativeMethods.TokenIsAppContainer);
        var hasRestrictions = ReadBool(token, NativeMethods.TokenHasRestrictions);
        var integrityRid = ReadIntegrityLevelRid(token);
        var userSid = ReadSid(token, NativeMethods.TokenUser);
        var appContainerSid = isAppContainer
            ? ReadAppContainerSid(token)
            : string.Empty;
        var restrictedSidCount = ReadSidCount(token, NativeMethods.TokenRestrictedSids);
        var capabilities = ReadSids(token, NativeMethods.TokenCapabilities);
        var enabledPrivilegeLuids = ReadEnabledPrivilegeLuids(token);
        var administratorsGroupEnabled = ReadAdministratorsGroupEnabled(token);

        return new TokenSecuritySnapshot(
            isAppContainer,
            hasRestrictions,
            integrityRid,
            IntegrityName(integrityRid),
            userSid,
            appContainerSid,
            restrictedSidCount,
            capabilities,
            enabledPrivilegeLuids,
            administratorsGroupEnabled);
    }

    internal static void RequireUntrustedExecutionToken(
        TokenSecuritySnapshot snapshot,
        string expectedAppContainerSid)
    {
        if (!snapshot.IsAppContainer)
            throw new BrokerSecurityException("Refusing execution: token is not an AppContainer token.");
        if (!string.Equals(
                snapshot.AppContainerSid,
                expectedAppContainerSid,
                StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("Refusing execution: token has the wrong AppContainer SID.");
        if (!snapshot.HasRestrictions)
            throw new BrokerSecurityException("Refusing execution: token has no restricted-token marker.");
        if (snapshot.AdministratorsGroupEnabled)
            throw new BrokerSecurityException(
                "Refusing execution: token has BUILTIN\\Administrators enabled.");
        foreach (var luid in snapshot.EnabledPrivilegeLuids)
        {
            if (luid != NativeMethods.SeChangeNotifyPrivilegeLuid)
                throw new BrokerSecurityException(
                    $"Refusing execution: token has an enabled privilege beyond SeChangeNotifyPrivilege (LUID {luid}).");
        }
        if (snapshot.Capabilities.Count != 0)
            throw new BrokerSecurityException(
                "Refusing execution: the AppContainer token unexpectedly has capabilities.");
        if (snapshot.IntegrityLevelRid > NativeMethods.SecurityMandatoryLowRid)
            throw new BrokerSecurityException(
                $"Refusing execution: token integrity is {snapshot.IntegrityLevel}, not low or untrusted.");
    }

    internal static SecurityIdentifier CurrentUserSid()
    {
        using var identity = WindowsIdentity.GetCurrent();
        return identity.User
            ?? throw new BrokerSecurityException("The broker process has no Windows user SID.");
    }

    internal static TokenSecuritySnapshot InspectCurrentProcess()
    {
        if (!NativeMethods.OpenProcessToken(
                NativeMethods.GetCurrentProcess(),
                NativeMethods.TokenQuery,
                out var token))
        {
            throw NativeMethods.LastError("OpenProcessToken(current process)");
        }

        using (token)
            return Inspect(token);
    }

    internal static void RequireWorkerToken(TokenSecuritySnapshot snapshot, string expectedAppContainerSid)
    {
        if (!snapshot.IsAppContainer
            || !snapshot.HasRestrictions
            || snapshot.Capabilities.Count != 0
            || snapshot.IntegrityLevelRid > NativeMethods.SecurityMandatoryLowRid)
        {
            throw new BrokerSecurityException(
                "Refusing workspace write: worker is not running in a restricted low-integrity AppContainer.");
        }
        if (!string.Equals(snapshot.AppContainerSid, expectedAppContainerSid, StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("Refusing workspace write: worker AppContainer identity is not operation-bound.");
    }

    private static bool ReadBool(SafeNativeHandle token, int informationClass)
    {
        return ReadInformation(token, informationClass, (buffer, _) => Marshal.ReadInt32(buffer) != 0);
    }

    private static string ReadSid(SafeNativeHandle token, int informationClass)
    {
        return ReadInformation(token, informationClass, (buffer, _) =>
        {
            var sidAndAttributes = Marshal.PtrToStructure<NativeMethods.SidAndAttributes>(buffer);
            return NativeMethods.SidToString(sidAndAttributes.Sid);
        });
    }

    private static string ReadAppContainerSid(SafeNativeHandle token)
    {
        return ReadInformation(token, NativeMethods.TokenAppContainerSid, (buffer, size) =>
        {
            if (buffer == IntPtr.Zero || size < IntPtr.Size)
                throw new BrokerSecurityException("Token has no AppContainer SID pointer.");
            return NativeMethods.SidToString(Marshal.ReadIntPtr(buffer));
        });
    }

    private static int ReadSidCount(SafeNativeHandle token, int informationClass)
    {
        return ReadInformation<int>(token, informationClass, (buffer, size) =>
        {
            if (buffer == IntPtr.Zero || size < sizeof(uint))
                return 0;
            return Marshal.ReadInt32(buffer);
        });
    }

    private static IReadOnlyList<string> ReadSids(SafeNativeHandle token, int informationClass)
    {
        return ReadInformation<IReadOnlyList<string>>(token, informationClass, (buffer, size) =>
        {
            if (buffer == IntPtr.Zero || size < sizeof(uint))
                return Array.Empty<string>();

            var count = Marshal.ReadInt32(buffer);
            if (count <= 0)
                return Array.Empty<string>();

            var offset = IntPtr.Size == 8 ? 8 : 4;
            var stride = Marshal.SizeOf<NativeMethods.SidAndAttributes>();
            var values = new List<string>(count);
            for (var index = 0; index < count; index++)
            {
                var entry = Marshal.PtrToStructure<NativeMethods.SidAndAttributes>(
                    IntPtr.Add(buffer, offset + index * stride));
                values.Add(NativeMethods.SidToString(entry.Sid));
            }

            return values;
        });
    }

    private static IReadOnlyList<long> ReadEnabledPrivilegeLuids(SafeNativeHandle token)
    {
        return ReadInformation<IReadOnlyList<long>>(token, NativeMethods.TokenPrivileges, (buffer, size) =>
        {
            if (buffer == IntPtr.Zero || size < sizeof(uint))
                throw new BrokerSecurityException("Token privilege list is missing.");

            var count = Marshal.ReadInt32(buffer);
            var stride = Marshal.SizeOf<NativeMethods.LuidAndAttributes>();
            if (count < 0 || sizeof(uint) + (long)count * stride > size)
                throw new BrokerSecurityException("Token privilege list is truncated.");

            var values = new List<long>();
            for (var index = 0; index < count; index++)
            {
                var entry = Marshal.PtrToStructure<NativeMethods.LuidAndAttributes>(
                    IntPtr.Add(buffer, sizeof(uint) + index * stride));
                if ((entry.Attributes & NativeMethods.SePrivilegeEnabled) != 0)
                    values.Add(((long)entry.HighPart << 32) | entry.LowPart);
            }

            return values;
        });
    }

    private static bool ReadAdministratorsGroupEnabled(SafeNativeHandle token)
    {
        return ReadInformation(token, NativeMethods.TokenGroups, (buffer, size) =>
        {
            if (buffer == IntPtr.Zero || size < sizeof(uint))
                throw new BrokerSecurityException("Token group list is missing.");

            var count = Marshal.ReadInt32(buffer);
            var offset = IntPtr.Size == 8 ? 8 : 4;
            var stride = Marshal.SizeOf<NativeMethods.SidAndAttributes>();
            if (count < 0 || offset + (long)count * stride > size)
                throw new BrokerSecurityException("Token group list is truncated.");

            for (var index = 0; index < count; index++)
            {
                var entry = Marshal.PtrToStructure<NativeMethods.SidAndAttributes>(
                    IntPtr.Add(buffer, offset + index * stride));
                if ((entry.Attributes & NativeMethods.SeGroupEnabled) != 0
                    && string.Equals(
                        NativeMethods.SidToString(entry.Sid),
                        NativeMethods.BuiltinAdministratorsSid,
                        StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }

            return false;
        });
    }

    private static int ReadIntegrityLevelRid(SafeNativeHandle token)
    {
        return ReadInformation(token, NativeMethods.TokenIntegrityLevel, (buffer, _) =>
        {
            var label = Marshal.PtrToStructure<NativeMethods.SidAndAttributes>(buffer);
            if (label.Sid == IntPtr.Zero)
                throw new BrokerSecurityException("Token has no integrity SID.");

            var countPointer = NativeMethods.GetSidSubAuthorityCount(label.Sid);
            if (countPointer == IntPtr.Zero)
                throw NativeMethods.LastError("GetSidSubAuthorityCount");

            var count = Marshal.ReadByte(countPointer);
            if (count == 0)
                throw new BrokerSecurityException("Token integrity SID has no sub-authorities.");

            var authority = NativeMethods.GetSidSubAuthority(label.Sid, (uint)(count - 1));
            if (authority == IntPtr.Zero)
                throw NativeMethods.LastError("GetSidSubAuthority");
            return Marshal.ReadInt32(authority);
        });
    }

    private static T ReadInformation<T>(
        SafeNativeHandle token,
        int informationClass,
        Func<IntPtr, uint, T> reader)
    {
        NativeMethods.GetTokenInformation(
            token,
            informationClass,
            IntPtr.Zero,
            0,
            out var required);
        var error = Marshal.GetLastWin32Error();
        if (required == 0 && error != NativeMethods.ErrorInsufficientBuffer)
        {
            if (error == 0)
                return reader(IntPtr.Zero, 0);
            throw new Win32Exception(error, $"GetTokenInformation({informationClass})");
        }

        var buffer = Marshal.AllocHGlobal(checked((int)required));
        try
        {
            if (!NativeMethods.GetTokenInformation(
                    token,
                    informationClass,
                    buffer,
                    required,
                    out var returned))
            {
                throw NativeMethods.LastError($"GetTokenInformation({informationClass})");
            }

            return reader(buffer, returned);
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static string IntegrityName(int rid) => rid switch
    {
        <= 0x0000 => "Untrusted",
        <= 0x1000 => "Low",
        <= 0x2000 => "Medium",
        <= 0x3000 => "High",
        _ => "System",
    };
}

internal static class WindowsAcl
{
    private const FileSystemRights ScratchRights =
        FileSystemRights.ReadAndExecute
        | FileSystemRights.Write
        | FileSystemRights.CreateFiles
        | FileSystemRights.CreateDirectories
        | FileSystemRights.ListDirectory;

    internal static ScratchSecuritySnapshot ProtectScratch(
        DirectoryInfo directory,
        SecurityIdentifier appContainerSid,
        SecurityIdentifier userSid)
    {
        var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        var administratorsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
        var security = new DirectorySecurity();
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
        AddRule(security, userSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, systemSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, administratorsSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, appContainerSid, ScratchRights, inherit: true);
        directory.SetAccessControl(security);

        var verified = directory.GetAccessControl(AccessControlSections.Access);
        var rules = verified
            .GetAccessRules(includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier))
            .OfType<FileSystemAccessRule>()
            .Where(rule => rule.AccessControlType == AccessControlType.Allow)
            .Select(rule => rule.IdentityReference.Value)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(value => value, StringComparer.OrdinalIgnoreCase)
            .ToArray();

        var appRule = verified
            .GetAccessRules(includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier))
            .OfType<FileSystemAccessRule>()
            .FirstOrDefault(rule =>
                rule.AccessControlType == AccessControlType.Allow
                && rule.IdentityReference.Value.Equals(appContainerSid.Value, StringComparison.OrdinalIgnoreCase));

        var canWrite = appRule is not null
            && (appRule.FileSystemRights & FileSystemRights.Write) == FileSystemRights.Write;
        if (!verified.AreAccessRulesProtected || !canWrite)
            throw new BrokerSecurityException("Scratch ACL verification failed closed.");

        return new ScratchSecuritySnapshot(
            directory.FullName,
            verified.AreAccessRulesProtected,
            appContainerSid.Value,
            rules,
            canWrite);
    }

    internal static void ProtectPayload(
        DirectoryInfo directory,
        SecurityIdentifier appContainerSid,
        SecurityIdentifier userSid)
    {
        var systemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        var administratorsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
        var security = new DirectorySecurity();
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
        AddRule(security, userSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, systemSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, administratorsSid, FileSystemRights.FullControl, inherit: true);
        AddRule(security, appContainerSid, FileSystemRights.ReadAndExecute, inherit: true);
        directory.SetAccessControl(security);

        var verified = directory.GetAccessControl(AccessControlSections.Access);
        var appRule = verified
            .GetAccessRules(includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier))
            .OfType<FileSystemAccessRule>()
            .FirstOrDefault(rule =>
                rule.AccessControlType == AccessControlType.Allow
                && rule.IdentityReference.Value.Equals(appContainerSid.Value, StringComparison.OrdinalIgnoreCase));
        if (!verified.AreAccessRulesProtected || appRule is null
            || (appRule.FileSystemRights & FileSystemRights.ReadAndExecute) != FileSystemRights.ReadAndExecute)
            throw new BrokerSecurityException("Payload ACL verification failed closed.");
    }

    private static void AddRule(
        DirectorySecurity security,
        SecurityIdentifier sid,
        FileSystemRights rights,
        bool inherit)
    {
        security.AddAccessRule(new FileSystemAccessRule(
            sid,
            rights,
            inherit ? InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit : InheritanceFlags.None,
            PropagationFlags.None,
            AccessControlType.Allow));
    }
}

internal sealed class WindowsPathGrants : IDisposable
{
    private sealed record AppliedGrant(string Path, FileSystemAccessRule Rule);

    private readonly List<AppliedGrant> applied = [];
    private bool disposed;

    private WindowsPathGrants(IReadOnlyList<FilesystemGrantEvidence> evidence)
    {
        Evidence = evidence;
    }

    internal IReadOnlyList<FilesystemGrantEvidence> Evidence { get; }

    internal static WindowsPathGrants Create(
        SecurityIdentifier appContainerSid,
        IEnumerable<(string Path, bool Writable)> requestedRoots)
    {
        return CreateWithRules(
            appContainerSid,
            requestedRoots.Select(pair => new WindowsPathGrantRequest(
                pair.Path,
                pair.Writable ? FileSystemRights.Modify : FileSystemRights.ReadAndExecute,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None)));
    }

    internal static WindowsPathGrants CreateWithRules(
        SecurityIdentifier appContainerSid,
        IEnumerable<WindowsPathGrantRequest> requestedRoots)
    {
        var roots = new Dictionary<
            (string Path, InheritanceFlags Inheritance, PropagationFlags Propagation),
            FileSystemRights>();
        foreach (var requestedRoot in requestedRoots)
        {
            var full = PathSafety.RequireLocalDirectory(
                requestedRoot.Path,
                "filesystem grant root");
            var key = (full, requestedRoot.Inheritance, requestedRoot.Propagation);
            if (roots.TryGetValue(key, out var existing))
                roots[key] = existing | requestedRoot.Rights;
            else
                roots.Add(key, requestedRoot.Rights);
        }

        var grants = new WindowsPathGrants(
            roots.Select(pair => new FilesystemGrantEvidence(
                pair.Key.Path,
                DescribeAccess(pair.Value),
                (int)pair.Value,
                pair.Key.Inheritance.ToString(),
                pair.Key.Propagation.ToString())).ToArray());
        try
        {
            foreach (var pair in roots)
            {
                grants.Apply(
                    pair.Key.Path,
                    pair.Value,
                    pair.Key.Inheritance,
                    pair.Key.Propagation,
                    appContainerSid);
            }
            return grants;
        }
        catch
        {
            grants.Dispose();
            throw;
        }
    }

    private void Apply(
        string path,
        FileSystemRights rights,
        InheritanceFlags inheritance,
        PropagationFlags propagation,
        SecurityIdentifier appContainerSid)
    {
        var rule = new FileSystemAccessRule(
            appContainerSid,
            rights,
            inheritance,
            propagation,
            AccessControlType.Allow);
        var directory = new DirectoryInfo(path);
        var security = directory.GetAccessControl(AccessControlSections.Access);
        security.AddAccessRule(rule);
        directory.SetAccessControl(security);

        var verified = directory.GetAccessControl(AccessControlSections.Access);
        var verifiedRule = verified
            .GetAccessRules(includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier))
            .OfType<FileSystemAccessRule>()
            .FirstOrDefault(candidate =>
                candidate.AccessControlType == AccessControlType.Allow
                && candidate.IdentityReference.Value.Equals(
                    appContainerSid.Value,
                    StringComparison.OrdinalIgnoreCase)
                && (candidate.FileSystemRights & rights) == rights);
        if (verifiedRule is null)
            throw new BrokerSecurityException(
                $"AppContainer filesystem grant could not be verified for {path}.");

        applied.Add(new AppliedGrant(path, rule));
    }

    public void Dispose()
    {
        if (disposed)
            return;

        disposed = true;
        Exception? failure = null;
        foreach (var grant in applied.AsEnumerable().Reverse())
        {
            try
            {
                var directory = new DirectoryInfo(grant.Path);
                var security = directory.GetAccessControl(AccessControlSections.Access);
                security.RemoveAccessRuleSpecific(grant.Rule);
                directory.SetAccessControl(security);
            }
            catch (Exception exception)
            {
                failure ??= new BrokerSecurityException(
                    $"Could not remove temporary AppContainer grant from {grant.Path}.",
                    exception);
            }
        }

        applied.Clear();
        if (failure is not null)
            throw failure;
    }

    private static string DescribeAccess(FileSystemRights rights)
    {
        if (rights == FileSystemRights.DeleteSubdirectoriesAndFiles)
            return "delete_child";
        if ((rights & FileSystemRights.Modify) == FileSystemRights.Modify)
            return "read_write";
        if ((rights & FileSystemRights.ReadAndExecute) == FileSystemRights.ReadAndExecute)
            return "read_only";
        return rights.ToString();
    }
}

internal sealed record WindowsPathGrantRequest(
    string Path,
    FileSystemRights Rights,
    InheritanceFlags Inheritance,
    PropagationFlags Propagation);

internal sealed class ScratchWorkspace : IDisposable
{
    private bool disposed;
    private readonly List<FileStream> stagedExecutableLocks = [];

    private ScratchWorkspace(string path, string payloadPath, ScratchSecuritySnapshot security)
    {
        Path = path;
        PayloadPath = payloadPath;
        Security = security;
    }

    internal string Path { get; }
    internal string PayloadPath { get; }
    internal ScratchSecuritySnapshot Security { get; }

    internal static ScratchWorkspace Create(
        SecurityIdentifier appContainerSid,
        SecurityIdentifier userSid)
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
            throw new BrokerSecurityException("Local application data is unavailable; refusing scratch creation.");
        var localRoot = System.IO.Path.GetPathRoot(localAppData);
        if (!System.IO.Path.IsPathFullyQualified(localAppData)
            || localAppData.StartsWith(@"\\", StringComparison.Ordinal)
            || string.IsNullOrWhiteSpace(localRoot)
            || NativeMethods.GetDriveTypeW(localRoot) != NativeMethods.DriveTypeFixed)
        {
            throw new BrokerSecurityException("Scratch must be on a local non-device drive.");
        }

        PathSafety.RejectReparseAncestors(localAppData);
        var voolRoot = System.IO.Path.Combine(localAppData, "VOOL");
        Directory.CreateDirectory(voolRoot);
        PathSafety.RejectReparseAncestors(voolRoot);
        var root = System.IO.Path.Combine(voolRoot, "broker-scratch");
        Directory.CreateDirectory(root);
        PathSafety.RejectReparseAncestors(root);
        var path = System.IO.Path.Combine(root, Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        PathSafety.RejectReparseAncestors(path);

        try
        {
            var directory = new DirectoryInfo(path);
            var security = WindowsAcl.ProtectScratch(directory, appContainerSid, userSid);
            var payloadPath = System.IO.Path.Combine(path, "payload");
            Directory.CreateDirectory(payloadPath);
            WindowsAcl.ProtectPayload(new DirectoryInfo(payloadPath), appContainerSid, userSid);
            return new ScratchWorkspace(path, payloadPath, security);
        }
        catch
        {
            DeleteTreeNoFollow(path);
            throw;
        }
    }

    internal string StageExecutable(Stream source)
    {
        if (disposed)
            throw new ObjectDisposedException(nameof(ScratchWorkspace));

        var stagedPath = System.IO.Path.Combine(PayloadPath, "payload.exe");
        FileStream? staged = new(stagedPath, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read);
        try
        {
            using var sourceHash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            var buffer = new byte[64 * 1024];
            while (true)
            {
                var read = source.Read(buffer, 0, buffer.Length);
                if (read == 0)
                    break;
                staged.Write(buffer, 0, read);
                sourceHash.AppendData(buffer, 0, read);
            }
            staged.Flush(flushToDisk: true);
            var attributes = File.GetAttributes(stagedPath);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new BrokerSecurityException("Refusing to execute a reparse-point payload.");

            // Transition to a read-only no-write/no-delete lock without ever permitting a
            // rename. A writer can race only during the transition; the final lock refuses to
            // open while such a writer exists, and the hash then catches a completed write.
            using var transition = new FileStream(
                stagedPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            staged.Dispose();
            staged = null;
            var locked = new FileStream(stagedPath, FileMode.Open, FileAccess.Read, FileShare.Read);
            try
            {
                var expectedHash = sourceHash.GetHashAndReset();
                var stagedHash = SHA256.HashData(locked);
                if (!CryptographicOperations.FixedTimeEquals(expectedHash, stagedHash))
                    throw new BrokerSecurityException("Staged executable changed before its launch lock was acquired.");
                locked.Position = 0;
                stagedExecutableLocks.Add(locked);
            }
            catch
            {
                locked.Dispose();
                throw;
            }
            return stagedPath;
        }
        catch
        {
            staged?.Dispose();
            throw;
        }
    }

    public void Dispose()
    {
        if (disposed)
            return;

        disposed = true;
        foreach (var stagedExecutableLock in stagedExecutableLocks)
            stagedExecutableLock.Dispose();
        stagedExecutableLocks.Clear();
        DeleteTreeNoFollow(Path);
    }

    private static void DeleteTreeNoFollow(string path)
    {
        if (!Directory.Exists(path))
            return;

        foreach (var entry in Directory.EnumerateFileSystemEntries(path))
        {
            var attributes = File.GetAttributes(entry);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                if ((attributes & FileAttributes.Directory) != 0)
                    Directory.Delete(entry, recursive: false);
                else
                    DeleteFileAfterImageRelease(entry);
                continue;
            }

            if ((attributes & FileAttributes.Directory) != 0)
                DeleteTreeNoFollow(entry);
            else
                DeleteFileAfterImageRelease(entry);
        }

        Directory.Delete(path, recursive: false);
    }

    private static void DeleteFileAfterImageRelease(string path)
    {
        for (var attempt = 0; attempt < 40; attempt++)
        {
            try
            {
                File.Delete(path);
                return;
            }
            catch (UnauthorizedAccessException) when (attempt < 39)
            {
                // Windows can release an exited process's mapped EXE after its Job count reaches zero.
                Thread.Sleep(50);
            }
        }
    }
}

internal static class PathSafety
{
    internal static string RequireLocalDirectory(string path, string label)
    {
        var full = RequireLocalPath(path, label);
        if (!Directory.Exists(full))
            throw new BrokerSecurityException($"{label} does not exist as a directory: {full}");
        return full.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
    }

    internal static string RequireLocalFile(string path, string label)
    {
        var full = RequireLocalPath(path, label);
        if (!File.Exists(full))
            throw new BrokerSecurityException($"{label} does not exist as a file: {full}");
        return full;
    }

    internal static string RequireLocalPath(string path, string label)
    {
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
            throw new BrokerSecurityException($"{label} must be an absolute path.");

        var full = Path.GetFullPath(path);
        var root = Path.GetPathRoot(full);
        if (string.IsNullOrWhiteSpace(root)
            || root.StartsWith(@"\\", StringComparison.Ordinal)
            || NativeMethods.GetDriveTypeW(root) != NativeMethods.DriveTypeFixed)
        {
            throw new BrokerSecurityException($"{label} must be on a fixed local volume: {full}");
        }

        RejectReparseAncestors(full);
        return full;
    }

    internal static bool IsWithin(string root, string candidate)
    {
        var normalizedRoot = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)
            + Path.DirectorySeparatorChar;
        var normalizedCandidate = Path.GetFullPath(candidate);
        return normalizedCandidate.StartsWith(normalizedRoot, StringComparison.OrdinalIgnoreCase)
            || string.Equals(
                normalizedCandidate,
                normalizedRoot.TrimEnd(Path.DirectorySeparatorChar),
                StringComparison.OrdinalIgnoreCase);
    }

    internal static bool IsWithinAny(IEnumerable<string> roots, string candidate) =>
        roots.Any(root => IsWithin(root, candidate));

    internal static void RejectReparseAncestors(string path)
    {
        var current = System.IO.Path.GetFullPath(path);
        while (!string.IsNullOrEmpty(current))
        {
            if (Directory.Exists(current) || File.Exists(current))
            {
                var attributes = File.GetAttributes(current);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new BrokerSecurityException($"Reparse-point path component refused: {current}");
            }

            var parent = Directory.GetParent(current)?.FullName;
            if (parent is null || string.Equals(parent, current, StringComparison.OrdinalIgnoreCase))
                break;
            current = parent;
        }
    }
}
