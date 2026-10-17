using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using Microsoft.Win32.SafeHandles;

namespace Vool.WindowsBroker;

// Retained object custody, not a pathname-based restoration fallback. All writes
// are DACL-only. Observed foreign drift refuses restoration; this is not an ACL CAS.
internal sealed class WindowsDacl : IDisposable
{
    private const uint DaclInformation = 4;
    private const uint OwnerAndDaclInformation = 1 | DaclInformation;
    private const uint NoFollowDirectoryFlags = 0x02000000 | 0x00200000;
    private const ControlFlags InheritanceConversion =
        ControlFlags.DiscretionaryAclAutoInherited | ControlFlags.DiscretionaryAclAutoInheritRequired;
    private readonly SafeFileHandle handle;

    private WindowsDacl(string path, SafeFileHandle handle)
    {
        Path = path;
        this.handle = handle;
        Identity = ReadIdentity(handle);
        Original = Read();
        if (Original.DiscretionaryAcl is null)
            throw new BrokerSecurityException("A non-null DACL is required for temporary grant custody.");
    }

    internal string Path { get; }
    internal string Identity { get; }
    internal RawSecurityDescriptor Original { get; }
    internal string OriginalAcl => Original.GetSddlForm(AccessControlSections.Access);

    internal static WindowsDacl Capture(string path)
    {
        var full = PathSafety.RequireLocalPath(path, "DACL custody path");
        var handle = Open(full, NativeMethods.ReadControl | NativeMethods.WriteDac);
        try
        {
            if (!ReadAttributeTag(handle, 9, out var tag, 8))
                throw NativeMethods.LastError("GetFileInformationByHandleEx for DACL custody");
            if ((tag.Attributes & 0x400) != 0)
                throw new BrokerSecurityException("Reparse-point DACL custody refused.");
            return new WindowsDacl(full, handle);
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    internal RawSecurityDescriptor Read()
    {
        // SE_FILE_OBJECT uses the same filesystem inheritance view as the final
        // path verification. The generic kernel-object query can omit inherited
        // ACE flags on children of old-model DACLs, corrupting our baseline.
        var error = GetSecurityInfo(handle, 1, OwnerAndDaclInformation,
            IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, out var descriptor);
        try
        {
            if (error != 0)
                throw new Win32Exception(checked((int)error), "GetSecurityInfo bound file DACL");
            if (descriptor == IntPtr.Zero)
                throw new BrokerSecurityException("The filesystem DACL query returned no descriptor.");
            var length = GetSecurityDescriptorLength(descriptor);
            if (length is < 20 or > 65536)
                throw new BrokerSecurityException("The filesystem DACL descriptor exceeds its size bound.");
            var bytes = new byte[checked((int)length)];
            Marshal.Copy(descriptor, bytes, 0, bytes.Length);
            return new RawSecurityDescriptor(bytes, 0);
        }
        finally
        {
            if (descriptor != IntPtr.Zero && NativeMethods.LocalFree(descriptor) != IntPtr.Zero)
                throw new BrokerSecurityException("The filesystem DACL query buffer could not be released.");
        }
    }

    internal void RequireBoundPath()
    {
        PathSafety.RejectReparseAncestors(Path);
        using var current = Open(Path, NativeMethods.ReadControl);
        if (!string.Equals(Identity, ReadIdentity(current), StringComparison.Ordinal)
            || !string.Equals(Identity, ReadIdentity(handle), StringComparison.Ordinal))
            throw new BrokerSecurityException("The DACL object identity changed.");
    }

    internal bool HasUnambiguousFileEncoding()
    {
        RequireBoundPath();
        var live = Read();
        if (live.GetSddlForm(AccessControlSections.Access) != OriginalAcl
            || live.Owner?.Value != Original.Owner?.Value)
            throw new BrokerSecurityException("Foreign DACL or owner drift prevents replacement admission.");

        // A legacy child can project inherited ACEs through SE_FILE_OBJECT while
        // storing explicit ACEs. ReplaceFile merges that encoding; do not mutate
        // a target whose exact original permissions cannot be preserved.
        var stored = ReadStored();
        RequireBoundPath();
        var final = Read();
        if (final.GetSddlForm(AccessControlSections.Access) != OriginalAcl
            || final.Owner?.Value != Original.Owner?.Value)
            throw new BrokerSecurityException("Foreign DACL or owner drift during replacement admission.");
        return stored.GetSddlForm(AccessControlSections.Access) == OriginalAcl
            && stored.Owner?.Value == Original.Owner?.Value;
    }

    internal RawSecurityDescriptor ReadStored()
    {
        if (GetKernelObjectSecurity(handle, OwnerAndDaclInformation, null, 0, out var length))
            throw new BrokerSecurityException("The stored DACL size query unexpectedly succeeded.");
        var error = Marshal.GetLastWin32Error();
        if (error != 122)
            throw new Win32Exception(error, "GetKernelObjectSecurity bound file DACL size");
        if (length is < 20 or > 65536)
            throw new BrokerSecurityException("The stored DACL descriptor exceeds its size bound.");
        var bytes = new byte[checked((int)length)];
        if (!GetKernelObjectSecurity(handle, OwnerAndDaclInformation, bytes, length, out var written))
            throw NativeMethods.LastError("GetKernelObjectSecurity bound file DACL");
        if (written is < 20 || written > length)
            throw new BrokerSecurityException("The stored DACL query returned an invalid length.");
        return new RawSecurityDescriptor(bytes, 0);
    }

    internal void WritePropagating(string acl, string expectedLiveAcl, string? expectedOwner)
    {
        RequireBoundPath();
        var live = Read();
        if (live.GetSddlForm(AccessControlSections.Access) != expectedLiveAcl
            || live.Owner?.Value != expectedOwner)
            throw new BrokerSecurityException("Foreign DACL or owner drift prevents grant propagation.");
        var descriptor = new RawSecurityDescriptor(acl);
        if (descriptor.DiscretionaryAcl is null)
            throw new BrokerSecurityException("Null DACL propagation refused.");
        var bytes = new byte[descriptor.BinaryLength];
        descriptor.GetBinaryForm(bytes, 0);
        var pinned = GCHandle.Alloc(bytes, GCHandleType.Pinned);
        try
        {
            if (!GetSecurityDescriptorDacl(pinned.AddrOfPinnedObject(), out var present, out var dacl, out _)
                || !present || dacl == IntPtr.Zero)
                throw new BrokerSecurityException("The propagation descriptor has no DACL.");
            var error = SetSecurityInfo(handle, 1, DaclInformation,
                IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
            if (error != 0)
                throw new Win32Exception(checked((int)error), "SetSecurityInfo DACL propagation");
        }
        finally
        {
            pinned.Free();
        }
    }

    internal void RestoreMetadata() => Restore(OriginalAcl, Original.Owner?.Value);

    internal void Restore(string originalAcl, string? expectedOwner, string? expectedInstalledAcl = null)
    {
        RequireBoundPath();
        var live = Read();
        var expected = new RawSecurityDescriptor(originalAcl);
        if (!string.Equals(live.Owner?.Value, expectedOwner, StringComparison.Ordinal)
            || (!EquivalentTransition(live, expected)
                && live.GetSddlForm(AccessControlSections.Access) != expectedInstalledAcl))
            throw new BrokerSecurityException("Foreign DACL or owner drift prevents exact restoration. "
                + $"Expected {originalAcl}; observed {live.GetSddlForm(AccessControlSections.Access)}; "
                + $"owner_match={live.Owner?.Value == expectedOwner}.");
        if (live.GetSddlForm(AccessControlSections.Access) != originalAcl)
        {
            if ((expected.ControlFlags & ControlFlags.DiscretionaryAclAutoInherited) != 0)
                expected.SetFlags(expected.ControlFlags | ControlFlags.DiscretionaryAclAutoInheritRequired);
            var bytes = new byte[expected.BinaryLength];
            expected.GetBinaryForm(bytes, 0);
            var status = NtSetSecurityObject(handle, DaclInformation, bytes);
            if (status != 0)
                throw new BrokerSecurityException($"Exact DACL restoration failed (NTSTATUS 0x{status:X8}).");
        }
        RequireBoundPath();
        var final = Read();
        if (final.GetSddlForm(AccessControlSections.Access) != originalAcl
            || !string.Equals(final.Owner?.Value, expectedOwner, StringComparison.Ordinal))
            throw new BrokerSecurityException("The bound final DACL or owner did not match its approved original. "
                + $"Expected {originalAcl}; observed {final.GetSddlForm(AccessControlSections.Access)}; "
                + $"owner_match={final.Owner?.Value == expectedOwner}.");
    }

    internal static bool EquivalentTransition(RawSecurityDescriptor live, RawSecurityDescriptor expected)
    {
        if ((live.ControlFlags & ~InheritanceConversion) != (expected.ControlFlags & ~InheritanceConversion)
            || live.DiscretionaryAcl is null || expected.DiscretionaryAcl is null)
            return false;
        var left = new byte[live.DiscretionaryAcl.BinaryLength];
        var right = new byte[expected.DiscretionaryAcl.BinaryLength];
        live.DiscretionaryAcl.GetBinaryForm(left, 0);
        expected.DiscretionaryAcl.GetBinaryForm(right, 0);
        return left.AsSpan().SequenceEqual(right);
    }

    internal static string ReadIdentity(SafeFileHandle handle)
    {
        if (!NativeMethods.GetFileInformationByHandleEx(handle, NativeMethods.FileIdInfoClass,
                out var identity, (uint)Marshal.SizeOf<NativeMethods.FileIdInfo>()) || identity.FileId is null)
            throw NativeMethods.LastError("GetFileInformationByHandleEx DACL identity");
        return $"{identity.VolumeSerialNumber:X16}:{Convert.ToHexString(identity.FileId)}";
    }

    private static SafeFileHandle Open(string path, uint access)
    {
        // Callers retain the canonical local path. Only the native open receives
        // an extended-length prefix; caller-supplied device/UNC paths stay refused.
        var nativePath = @"\\?\" + path;
        var result = NativeMethods.CreateFileForSecurityW(nativePath, access | 0x80, // FILE_READ_ATTRIBUTES.
            NativeMethods.FileShareRead | NativeMethods.FileShareWrite | NativeMethods.FileShareDelete,
            IntPtr.Zero, NativeMethods.OpenExisting, NoFollowDirectoryFlags, IntPtr.Zero);
        if (!result.IsInvalid)
            return result;
        var error = Marshal.GetLastWin32Error();
        result.Dispose();
        throw new Win32Exception(error,
            $"CreateFileW for bound DACL: native_error={error}; path={path}; access=0x{access | 0x80:X8}");
    }

    public void Dispose() => handle.Dispose();

    [StructLayout(LayoutKind.Sequential)]
    private struct AttributeTag { internal uint Attributes; internal uint ReparseTag; }

    [DllImport("kernel32.dll", EntryPoint = "GetFileInformationByHandleEx", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ReadAttributeTag(SafeFileHandle handle, int informationClass, out AttributeTag tag, uint size);

    [DllImport("advapi32.dll")]
    private static extern uint GetSecurityInfo(SafeFileHandle handle, int objectType, uint information,
        IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl, out IntPtr descriptor);

    [DllImport("advapi32.dll")]
    private static extern uint GetSecurityDescriptorLength(IntPtr descriptor);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetKernelObjectSecurity(SafeFileHandle handle, uint information,
        [Out] byte[]? descriptor, uint length, out uint needed);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetSecurityDescriptorDacl(IntPtr descriptor,
        [MarshalAs(UnmanagedType.Bool)] out bool present, out IntPtr dacl, [MarshalAs(UnmanagedType.Bool)] out bool defaulted);

    [DllImport("advapi32.dll")]
    private static extern uint SetSecurityInfo(SafeFileHandle handle, int objectType, uint information,
        IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

    [DllImport("ntdll.dll", ExactSpelling = true)]
    private static extern int NtSetSecurityObject(SafeFileHandle handle, uint information, byte[] descriptor);
}
